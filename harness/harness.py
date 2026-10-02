"""The orchestrator. It passes work between the agents and keeps state. It makes no
model calls of its own: each agent (Router, Planner, Q&A, Reviewer) runs its own loop.

Everything a run needs to continue is stored in the drive:

    goals/g-*.json       what was asked, by whom, and the goal's slots
    runs/r-*.json        the Planner's full conversation, so a paused run can resume
    questions/q-*.json   questions to scientists, their choices and answers
    messages/<id>.json   each scientist's inbox
    trace/r-*.jsonl      every model call and tool call in a run
    findings/            findings.md, its history, notes for file owners, plate plans

Entry points, called by the UI or a script: `message`, `answer`, `upload`.

How a run moves:
    message --Router--> new goal --> Planner loop --+--> ask:    Q&A writes questions, run waits
                                          ^         |            answer() resumes the Planner
                                          |         +--> submit: Reviewer passes or fails it
                                          +--fail---+            pass: findings + Q&A report
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from harness.agents.extractor import llm_protocol_extractor
from harness.agents.planner import Handoff, Planner, tool_reply
from harness.agents import qa as qa_module
from harness.agents.qa import QA
from harness.agents.reviewer import Reviewer
from harness.agents.router import Router
from harness.drive.store import Drive
from harness.gateway.gateway import Gateway, jsonl_tracer
from harness.state import REQUIRED_SLOTS, RunState
from harness.tools import files
from harness.tools.analysis import STATS


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Harness:
    def __init__(self, drive: Drive, gateway: Gateway, max_turns: int = 40,
                 max_revisions: int = 2, tool_budget: int = 25):
        self.drive, self.gw = drive, gateway
        self.max_revisions, self.tool_budget = max_revisions, tool_budget
        extractor = llm_protocol_extractor(gateway)
        self.router = Router(gateway)
        self.qa = QA(gateway)
        self.reviewer = Reviewer(gateway)
        self.planner = Planner(gateway, drive, max_turns=max_turns, overrides={
            "extract_protocol_fields":
                lambda d, path: files.extract_protocol_fields(d, path, extractor=extractor)})

    # ---- small stores -------------------------------------------------------------

    def _ids(self, folder: str) -> list[str]:
        d = self.drive.path(folder)
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def _new_id(self, folder: str, prefix: str) -> str:
        return f"{prefix}-{len(self._ids(folder)) + 1:03d}"

    def _load(self, folder: str, id_: str) -> dict:
        return self.drive.read_json(f"{folder}/{id_}.json")

    def _save(self, folder: str, obj: dict) -> None:
        self.drive.write_json(f"{folder}/{obj['id']}.json", obj)

    def goals(self) -> list[dict]:
        return [self._load("goals", g) for g in self._ids("goals")]

    def questions(self, person: str | None = None, status: str | None = None) -> list[dict]:
        qs = [self._load("questions", q) for q in self._ids("questions")]
        return [q for q in qs if (person is None or q["to"] == person)
                and (status is None or q["status"] == status)]

    def inbox(self, person: str) -> list[dict]:
        return self.drive.read_json(f"messages/{person}.json", [])

    def _notify(self, person: str, kind: str, text: str, ref: str | None = None) -> None:
        """Deliver a message to an inbox. `text` always comes from the Q&A agent. Locked,
        because a run and a chat answer can both write to the same inbox."""
        if person not in self.drive.people:
            return
        entry = {"at": _now(), "kind": kind, "text": text, "ref": ref}
        self.drive.update_json(f"messages/{person}.json", lambda box: (box or []) + [entry], [])

    def _person(self, pid: str) -> dict:
        return {"id": pid, **self.drive.people.get(pid, {"name": pid})}

    def _trace(self, run: dict, event: dict) -> None:
        jsonl_tracer(self.drive.path(f"trace/{run['id']}.jsonl"))(event)

    # ---- progress threads -----------------------------------------------------------
    # One thread per request (or upload). The UI shows it live, like a chat reply. Every
    # line comes from the Q&A agent's fixed progress templates: instant, no model call.

    def receive(self, sender: str, text: str, kind: str = "request") -> str:
        """Acknowledge a request at once, before any model call. Returns the thread id."""
        if sender not in self.drive.people:
            raise ValueError(f"unknown person {sender!r}")
        pid = self._new_id("progress", "p")
        self._save("progress", {"id": pid, "by": sender, "kind": kind, "text": text, "goal": None,
                                "state": "working", "started": _now(), "steps": []})
        if kind == "request":
            self._say(pid, "received", text=text)
        return pid

    def threads(self, person: str | None = None) -> list[dict]:
        """Progress threads, oldest first: a person's own, and those for goals they asked."""
        out = []
        for t in (self._load("progress", i) for i in self._ids("progress")):
            if t is None:
                continue
            goal = self._load("goals", t["goal"]) if t.get("goal") else None
            if person is None or t["by"] == person or (goal and goal["requester"] == person):
                out.append(t)
        return out

    def _say(self, pid: str | None, kind: str, state: str | None = None, **details) -> None:
        """Add a Q&A progress line of template `kind` to a thread."""
        self._step(pid, self.qa.progress(kind, **details), state,
                   milestone=kind in qa_module.MILESTONES)

    def _step(self, pid: str | None, text: str, state: str | None = None,
              milestone: bool = True, **fields) -> None:
        if not pid:
            return
        t = self._load("progress", pid)
        if t is None:
            return
        t["steps"].append({"at": _now(), "text": text, "milestone": milestone})
        if state:
            t["state"] = state
        t.update(fields)
        self._save("progress", t)

    # ---- entry points -------------------------------------------------------------

    def message(self, sender: str, text: str, progress_id: str | None = None) -> dict:
        """A scientist typed something: route it, answer it, or start an analysis.
        Scripts and tests use this; the UI calls `chat` and `start_analysis` separately so a
        question can be answered while an analysis is running."""
        pid = progress_id or self.receive(sender, text)
        out = self.chat(sender, text, pid)
        if out["intent"] != "new_analysis":
            return out
        return self.start_analysis(sender, text, out["slots"], out.get("unclear", []), pid)

    def active_analyses(self, person: str | None = None) -> list[dict]:
        """Goals whose latest run is still going, that involve `person`: they asked it, or
        a question in it is for them or copies them."""
        out = []
        for goal in self.goals():
            run = self._load("runs", goal["runs"][-1]) if goal["runs"] else None
            qs = [q for q in self.questions() if q["goal_id"] == goal["id"]]
            # In progress while the run is going, or while anything on it is still open
            # (e.g. a plan waiting for approval after the run finished).
            if not run or (run["status"] not in ("running", "waiting")
                           and not any(q["status"] == "open" for q in qs)):
                continue
            if person is None or goal["requester"] == person or any(
                    q["to"] == person or person in q["cc"] for q in qs):
                waiting = self._waiting_for([q["id"] for q in qs if q["status"] == "open"]) \
                    if any(q["status"] == "open" for q in qs) else ""
                out.append({"goal": goal["id"], "question": goal["question"],
                            "status": run["status"] if run["status"] in ("running", "waiting") else "awaiting answers",
                            "terms": {k: v for k, v in goal["slots"].items() if k in TERM_SLOTS},
                            "waiting_for": waiting})
        return out

    def chat(self, sender: str, text: str, pid: str) -> dict:
        """Route a message and answer it if it's a question. Read-only for every analysis,
        so it can run while one is in progress. A new analysis is returned, not started."""
        try:
            active = self.active_analyses(sender)
            routed = self.router.run(self._person(sender), text, active=active)
            intent = routed["intent"]
            # Code fallback: with an analysis running, a message about the same compound (or
            # none) is about that analysis, not a second one.
            if active and intent == "new_analysis":
                asked = routed["slots"].get("compound")
                if not asked or any(_norm(asked) == _norm(a["terms"].get("compound", ""))
                                    for a in active):
                    intent = "question_about_run"
            if active and intent == "question_about_findings" and not self.drive.path(
                    "findings/findings.md").exists():
                intent = "question_about_run"
            if active and intent == "other" and _looks_like_question(text):
                intent = "question_about_run"

            goal = self._goal_for_questions(sender, active)
            if intent in ("question_about_findings", "question_about_run") and goal:
                # One state for any question about an analysis: its findings (if any), what
                # the run did, the questions and answers, and any plate plan in full.
                self._say(pid, "looking")
                reply = self.qa.explain(self._person(sender), text, self.run_state(goal, reader=sender),
                                        renamed=_renamed(goal))
                self._notify(sender, "answer", reply)
                self._step(pid, reply, state="done", goal=goal["id"])
                return {"intent": intent, "reply": reply, "progress": pid}
            if intent in ("question_about_findings", "question_about_run"):
                self._say(pid, "answering")
                findings = self.drive.path("findings/findings.md")
                reply = self.qa.answer(self._person(sender), text,
                                       findings.read_text() if findings.exists() else "")
                self._notify(sender, "answer", reply)
                self._step(pid, reply, state="done")
                return {"intent": intent, "reply": reply, "progress": pid}
            if intent != "new_analysis":
                reply = self.qa.notice("not_a_request")
                self._notify(sender, "notice", reply)
                self._step(pid, reply, state="done")
                return {"intent": intent, "reply": reply, "progress": pid}
            return {"intent": "new_analysis", "slots": routed["slots"],
                    "unclear": routed.get("unclear", []), "progress": pid}
        except Exception as exc:
            self._say(pid, "failed", state="failed", reason=f"{type(exc).__name__}: {exc}")
            raise

    def _goal_for_questions(self, sender: str, active: list[dict]) -> dict | None:
        """The analysis a question is most likely about: one in progress for this person,
        else the most recent one they asked or took part in."""
        if active:
            return self._load("goals", active[0]["goal"])
        involved = {q["goal_id"] for q in self.questions() if q["to"] == sender or sender in q["cc"]}
        mine = [g for g in self.goals() if g["requester"] == sender or g["id"] in involved]
        return mine[-1] if mine else None

    def start_analysis(self, sender: str, text: str, slots: dict, unclear: list[str],
                       pid: str) -> dict:
        """Create the goal and run it. The heavy part: one at a time."""
        try:
            compound = slots.get("compound")
            self._say(pid, "understood", about=f" of {compound}" if compound else "")
            goal = {"id": self._new_id("goals", "g"), "requester": sender, "question": text,
                    "slots": dict(slots), "asked_slots": dict(slots), "unclear_slots": list(unclear),
                    "status": "open", "runs": [], "history": [], "created": _now()}
            self._save("goals", goal)
            t = self._load("progress", pid)
            t["goal"] = goal["id"]
            self._save("progress", t)
            return {"intent": "new_analysis", "goal": goal["id"], "progress": pid,
                    **self._start_run(goal, f"{self._person(sender)['name']} asked: {text}", pid)}
        except Exception as exc:
            self._say(pid, "failed", state="failed", reason=f"{type(exc).__name__}: {exc}")
            raise

    def _analysis_so_far(self) -> dict:
        """Which plates the current result used, and which were excluded, by whom and why."""
        stats = self.drive.read_json(STATS) or {}
        contradictions = {c["id"]: c for c in self.drive.contradictions()}
        excluded = []
        for f in self.drive.facts():
            plate = (f.get("action") or {}).get("exclude_plate")
            if not plate:
                continue
            c = contradictions.get(f.get("resolves"), {})
            why = "; ".join(f"{i['field']} was {i['data_value']} (protocol: {i['protocol_value']})"
                            for i in c.get("issues", []))
            excluded.append({"plate": plate, "file": f["scope"]["file"], "batch": f["scope"]["batch"],
                             "decided_by": self._person(f["confirmed_by"])["name"], "why": why})
        return {
            "plates_in_current_result": [k.split(":")[1] + f" ({k.split(':')[0]})"
                                         for k in stats.get("plates_used", [])],
            "plates_left_out_for_no_vehicle": stats.get("plates_refused_no_vehicle", []),
            "excluded_by_the_team": excluded,
            "result": {k: stats.get(k) for k in ("drop", "p", "meets_threshold")} if stats else None,
        }

    def _plan_purpose(self, meta: dict) -> str:
        """What a plate plan is for, and what the analysis will use once its data arrive."""
        so_far = self._analysis_so_far()
        used = [p.split(" ")[0] for p in so_far["plates_in_current_result"]]
        out = [x["plate"] for x in so_far["excluded_by_the_team"]] + \
              [k.split(":")[1] for k in so_far["plates_left_out_for_no_vehicle"]]
        if meta.get("kind") == "rerun":
            redo = f"Re-does the work of {', '.join(sorted(set(out)))}, which was left out of the analysis, " \
                if out else "Repeats the experiment "
            return (redo + f"properly: {meta.get('treatment_hours', 72)} h, with vehicle on the same plate. "
                    f"It is not a repeat of {', '.join(used) or 'the analysed plates'}. Once its data are "
                    f"uploaded, the analysis will use {' and '.join(used + ['the new plate']) if used else 'the new plate'}"
                    + (f"; {', '.join(sorted(set(out)))} stays excluded." if out else "."))
        return (f"Measures how potent {meta.get('compound')} is across 10 doses, after the result met the "
                "threshold. It doesn't change the current result.")

    def run_state(self, goal: dict, reader: str | None = None) -> dict:
        """What an analysis has done so far, in plain terms, for the Q&A agent to explain."""
        plans = []
        folder = self.drive.path("findings/plans")
        for meta_path in sorted(folder.glob("*.json")) if folder.exists() else []:
            if meta_path.name.endswith(".lineage.json"):
                continue
            meta = json.loads(meta_path.read_text())
            if meta.get("goal_id") != goal["id"]:
                continue
            import pandas as pd
            layout = pd.read_csv(self.drive.path(meta["plan"]))
            wells = layout.groupby(["condition", "conc_uM"]).size().reset_index(name="wells")
            plans.append({**meta, "wells_by_condition": wells.to_dict(orient="records"),
                          "purpose": meta.get("purpose") or self._plan_purpose(meta)})
        thread = next((t for t in reversed(self.threads()) if t.get("goal") == goal["id"]), None)
        claims = self.drive.read_json(files.NOTEBOOK_CLAIMS, {}) or {}
        findings = self.drive.path("findings/findings.md")
        return {
            "reader": {"name": self._person(reader)["name"] if reader else None,
                       "is_the_requester": reader == goal["requester"]},
            "findings": findings.read_text() if findings.exists() and goal.get("last_run") else None,
            "request": goal["question"],
            "requester": self._person(goal["requester"])["name"],
            "analysis_so_far": self._analysis_so_far(),
            "confirmed_terms": {k: v for k, v in goal["slots"].items() if k in TERM_SLOTS},
            "threshold": goal["slots"].get("threshold"),
            "progress_so_far": [x["text"] for x in (thread or {}).get("steps", []) if x.get("milestone", True)],
            "questions": [{"to": self._person(q["to"])["name"], "for_the_reader": q["to"] == reader,
                           "about": q["about"], "status": q["status"],
                           "asked": q["message"], "answer": (q.get("answer") or {}).get("statement")
                           or (q.get("answer") or {}).get("choice")}
                          for q in self.questions() if q["goal_id"] == goal["id"]],
            "contradictions": self.drive.contradictions(),
            "decisions": self.drive.facts(),
            "plate_plans": plans,
            "stats": self.drive.read_json(STATS),
            "notebook_claim": {k: claims.get(k) for k in ("claimed_drop", "claimed_p", "claimed_verdict",
                                                         "normalises_per_plate")} if claims else None,
        }

    def upload(self, src, owner: str, description: str, replace: bool = False,
               reevaluate: bool = True) -> dict:
        """A scientist added (or replaced) a file. Any file type. By default every goal that
        already has a run is re-evaluated with it."""
        rel = self.drive.upload(src, owner, description, replace=replace)
        what = "replaced" if replace else "uploaded"
        if not reevaluate:
            self._mark_stale(f"{rel} {what} by {self._person(owner)['name']}")
            return {"uploaded": rel, "runs": []}
        return {"uploaded": rel, "runs": self.reevaluate(
            f"File {what} by {self._person(owner)['name']}: {rel}.", by=owner, uploaded=rel)}

    def remove(self, rel: str, by: str) -> dict:
        """The file's owner removed it. It goes to trash/, and findings that used it are
        marked out of date. No run starts: a person decides when to re-evaluate."""
        result = self.drive.remove(rel, by)
        self._mark_stale(f"{rel} removed by {self._person(by)['name']}")
        return result

    def reevaluate(self, reason: str, by: str | None = None, uploaded: str | None = None) -> list[dict]:
        """Start a new run for every goal that already has one, each with its own thread."""
        runs = []
        for goal in self.goals():
            if goal["runs"]:
                goal["stale"] = []
                self._save("goals", goal)
                who = by or goal["requester"]
                pid = self.receive(who, f"Uploaded {uploaded}" if uploaded else reason,
                                   kind="upload" if uploaded else "reevaluate")
                t = self._load("progress", pid)
                t["goal"] = goal["id"]
                self._save("progress", t)
                if uploaded:
                    self._say(pid, "uploaded", name=self._person(who)["name"], file=uploaded,
                              question=goal["question"])
                else:
                    self._say(pid, "reevaluating", question=goal["question"])
                runs.append(self._start_run(goal, f"{reason} Re-evaluate the goal with the files "
                                                  "as they are now. Reuse facts the team already confirmed.",
                                            pid))
        return runs

    def _mark_stale(self, change: str) -> None:
        for goal in self.goals():
            if goal["status"] == "answered":
                goal.setdefault("stale", []).append({"change": change, "at": _now()})
                self._save("goals", goal)

    def answer(self, qid: str, by: str, choice: str, text: str | None = None) -> dict:
        """A scientist answered a question, by choice key ("other" with `text` for free text)."""
        q = self._load("questions", qid)
        if q is None:
            raise ValueError(f"no question {qid}")
        if q["status"] != "open":
            return {"recorded_as": "nothing", "reason": f"{qid} is already {q['status']}"}
        keys = {c["key"] for c in q["choices"]}
        if choice not in keys:
            raise ValueError(f"{choice!r} isn't one of {sorted(keys)}")
        kind, _, ref = q["about"].partition(":")

        if choice == "other" and not (text or "").strip():
            raise ValueError("'other' needs the answer typed in `text`")
        if kind == "contradiction":
            res = self.drive.resolve_contradiction(ref, by, text if choice == "other" else choice)
            if res["recorded_as"] == "comment":
                return res
            recorded = {"fact": res["fact"]["id"], "statement": res["fact"]["statement"]}
        elif by != q["to"]:
            return {"recorded_as": "comment",
                    "reason": f"{qid} is for {q['to']}, so only they can answer it"}
        elif kind == "slot":
            value = next(c.get("value") for c in q["choices"] if c["key"] == choice)
            if choice == "other" and ref != "threshold":
                value = self.qa.parse_other(q, text or "")["value"]
                if not isinstance(value, str) or not value.strip():
                    self._notify(by, "notice", self.qa.notice(
                        "unreadable_answer", what=f"a {ref}",
                        example=f"one of: {', '.join(c['label'] for c in q['choices'] if c['key'] != 'other')}"), qid)
                    return {"recorded_as": "nothing", "reason": "couldn't understand the answer"}
                value = value.strip()
            elif choice == "other":
                parsed = self.qa.parse_other(q, text or "")
                value = parsed["value"]
                if not _valid_slot(ref, value):
                    self._notify(by, "notice", self.qa.notice(
                        "unreadable_answer", what="a threshold",
                        example="'a 30% drop with p below 0.05'"), qid)
                    return {"recorded_as": "nothing", "reason": "couldn't parse the answer"}
            goal = self._load("goals", q["goal_id"])
            goal["slots"][ref] = value
            goal.get("ungrounded", {}).pop(ref, None)
            goal.setdefault("confirmed_slots", []).append(ref)   # the requester's word stands
            self._save("goals", goal)
            fact = self.drive.add_fact({"goal": goal["id"], "slot": ref, "value": value,
                                        "statement": f"{ref}: {_slot_text(ref, value)}",
                                        "confirmed_by": by})
            recorded = {"fact": fact["id"], "statement": fact["statement"]}
        elif kind == "approval":
            plan_meta = ref.replace(".csv", ".json")
            meta = self.drive.read_json(plan_meta)
            meta.update(status="approved" if choice == "approve" else "rejected",
                        decided_by=by, decided_at=_now())
            self.drive.write_json(plan_meta, meta)
            recorded = {"plan": ref, "status": meta["status"]}
            self._rerender(q["goal_id"])
        else:
            raise ValueError(f"unknown question kind {kind!r}")

        q.update(status="answered", answer={"by": by, "choice": choice, "text": text,
                                            "at": _now(), **recorded})
        self._save("questions", q)
        run = self._load("runs", q["run_id"]) or {}
        if run.get("status") == "waiting":
            still_open = [qid_ for qid_ in run["pending"]["questions"]
                          if self._load("questions", qid_)["status"] == "open"]
            if still_open:
                self._say(run.get("progress_id"), "answered_still_waiting", state="waiting",
                          name=self._person(by)["name"], who=self._waiting_for(still_open))
            else:
                self._say(run.get("progress_id"), "all_answered", state="working",
                          name=self._person(by)["name"])
        return {"recorded_as": "fact" if "fact" in recorded else "decision", **recorded,
                **self._maybe_resume(q["run_id"])}

    # ---- runs -----------------------------------------------------------------------

    def _start_run(self, goal: dict, trigger: str, progress_id: str | None = None) -> dict:
        run = {"id": self._new_id("runs", "r"), "goal_id": goal["id"], "trigger": trigger,
               "status": "running", "tool_calls": 0, "turns": 0, "revisions": 0, "nudges": 0,
               "pending": None, "started": _now(), "progress_id": progress_id,
               "messages": Planner.first_messages(self._context(goal, trigger))}
        goal["runs"].append(run["id"])
        self._save("goals", goal)
        self._save("runs", run)
        return self._loop(run)

    def _maybe_resume(self, run_id: str) -> dict:
        run = self._load("runs", run_id)
        if not run or run["status"] != "waiting":
            return {}
        pending = [self._load("questions", q) for q in run["pending"]["questions"]]
        if any(q["status"] == "open" for q in pending):
            return {"run": run_id, "status": "waiting",
                    "still_open": [q["id"] for q in pending if q["status"] == "open"]}
        answers = [{"question": q["id"], "about": q["about"], "answered_by": q["answer"]["by"],
                    "answer": q["answer"].get("statement") or q["answer"]["choice"]}
                   for q in pending]
        tool_reply(run, run["pending"]["tool_call_id"], {"ok": True, "answers": answers})
        run.update(status="running", pending=None)
        self._trace(run, {"event": "resumed", "answers": answers})
        return self._loop(run)

    def _context(self, goal: dict, trigger: str) -> str:
        findings = self.drive.path("findings/findings.md")
        return json.dumps({
            "trigger": trigger,
            "goal": {"id": goal["id"], "question": goal["question"],
                     "requester": self._person(goal["requester"]), "known_slots": goal["slots"],
                     "required_slots": list(REQUIRED_SLOTS)},
            "people": self.drive.people,
            "files": self.drive.files(),
            "confirmed_facts": self.drive.facts(),
            "open_contradictions": self.drive.open_contradictions(),
            "previous_verdict": goal["history"][-1] if goal["history"] else None,
            "previous_findings_exist": findings.exists(),
        }, indent=2, default=str)

    def _save_run(self, run: dict) -> None:
        run["tokens_used"] = self.gw.tokens_used
        self._save("runs", run)

    def _finish(self, run: dict, status: str, note: str = "") -> dict:
        run.update(status=status, finished=_now(), note=note)
        self._save_run(run)
        self._trace(run, {"event": "finished", "status": status, "note": note,
                          "tokens_used": self.gw.tokens_used})
        return {"run": run["id"], "status": status, "note": note}

    def _loop(self, run: dict) -> dict:
        """Drive a run. An unexpected error fails the run cleanly: the requester is told,
        the progress thread says so, and nothing is left stuck as "running"."""
        try:
            return self._drive(run)
        except Exception as exc:                                         # noqa: BLE001
            reason = f"an internal error ({type(exc).__name__}: {exc})"
            goal = self._load("goals", run["goal_id"]) or {}
            if goal.get("requester"):
                self._notify(goal["requester"], "notice", self.qa.notice("run_stopped", reason=reason),
                             run["id"])
            self._say(run.get("progress_id"), "failed", state="failed", reason=reason)
            self._trace(run, {"event": "error", "error": reason})
            return self._finish(run, "failed", reason)

    def _drive(self, run: dict) -> dict:
        """Run the Planner, and act on each handoff until the run waits or finishes."""
        goal = self._load("goals", run["goal_id"])
        self.gw.tokens_used = run.get("tokens_used", 0)     # each run has its own token budget
        self.gw.tracer = jsonl_tracer(self.drive.path(f"trace/{run['id']}.jsonl"))
        state = RunState(goal=goal["slots"], budget=self.tool_budget, tool_calls=run["tool_calls"],
                         ungrounded=goal.get("ungrounded", {}))
        pid = run.get("progress_id")

        def on_tool(name: str, args: dict, result: dict) -> None:
            if result.get("ok"):
                self._say(pid, name, path=args.get("path", ""), name=args.get("name", ""))
            self._after_tool(run, goal, name, args, result)
            state.ungrounded = goal.get("ungrounded", {})
            self._trace(run, {"event": "tool", "tool": name, "args": args, "ok": result.get("ok"),
                              "blocked": result.get("blocked"), "error": result.get("error")})

        while True:
            handoff: Handoff = self.planner.run(run, state, on_tool)
            if handoff.kind == "failed":
                self._notify(goal["requester"], "notice",
                             self.qa.notice("run_stopped", reason=handoff.reason), run["id"])
                self._say(pid, "failed", state="failed", reason=handoff.reason)
                return self._finish(run, "failed", handoff.reason)

            if handoff.kind == "ask":
                self._say(pid, "asking")
                created, errors = self._create_questions(run, goal, handoff.args)
                if not created:
                    tool_reply(run, handoff.call_id, {"ok": False, "errors": errors})
                    self._save_run(run)
                    continue
                run.update(status="waiting", pending={"tool_call_id": handoff.call_id,
                                                      "questions": created, "errors": errors})
                self._save_run(run)
                self._trace(run, {"event": "waiting", "questions": created, "errors": errors})
                self._say(pid, "waiting", state="waiting", who=self._waiting_for(created))
                return {"run": run["id"], "status": "waiting", "questions": created}

            self._say(pid, "reviewing", state="working")
            done = self._review_and_finish(run, goal, handoff.args)
            if done:
                return done
            self._say(pid, "revising")
            tool_reply(run, handoff.call_id, {"ok": False, "review": run["last_review"],
                                              "revisions_left": self.max_revisions - run["revisions"]})
            self._save_run(run)

    def _waiting_for(self, qids: list[str]) -> str:
        """'Ben Okafor on plate P2, Ana Ruiz on the threshold'."""
        parts = []
        for qid in qids:
            q = self._load("questions", qid)
            kind, _, ref = q["about"].partition(":")
            if kind == "contradiction":
                c = next((c for c in self.drive.contradictions() if c["id"] == ref), {})
                what = f"plate {c.get('plate_id', ref)}"
            elif kind == "slot":
                what = f"the {ref}"
            else:
                what = "a plan approval"
            parts.append(f"{self._person(q['to'])['name']} on {what}")
        return ", ".join(parts)

    def _known_values(self) -> dict[str, list[str]]:
        """What the files say for each goal slot: compounds in the readings and protocol,
        the protocol's readout, and the comparisons the data and protocol support."""
        conditions = []
        readings = self.drive.path("derived/clean_readings.csv")
        if readings.exists():
            import pandas as pd
            conditions = list(pd.read_csv(readings).condition.unique())
        protocol = self.drive.read_json(files.PROTOCOL, {}) or {}
        # What's in the data comes first: the protocol's wording is only used when the data
        # has nothing for that slot, so the options aren't the same thing phrased three ways.
        in_data = {"compound": [c for c in conditions if c != "vehicle"],
                   "comparator": [c for c in conditions if c == "vehicle"], "readout": []}
        in_protocol = {"compound": [protocol.get("compound")], "readout": [protocol.get("readout")],
                       "comparator": [protocol.get("comparator"), protocol.get("control")]}
        known = {}
        for slot in ("compound", "readout", "comparator"):
            values = sorted({v for v in in_data[slot] if v}) or sorted({v for v in in_protocol[slot] if v})
            known[slot] = values
        return known

    def _match_terms(self) -> dict[str, list[str]]:
        """Every name the files use for each slot, data and protocol, for matching what the
        requester wrote. Wider than `_known_values`, which is what's offered as options."""
        conditions = []
        readings = self.drive.path("derived/clean_readings.csv")
        if readings.exists():
            import pandas as pd
            conditions = list(pd.read_csv(readings).condition.unique())
        protocol = self.drive.read_json(files.PROTOCOL, {}) or {}
        terms = {"compound": [c for c in conditions if c != "vehicle"] + [protocol.get("compound")],
                 "readout": [protocol.get("readout")],
                 "comparator": [c for c in conditions if c == "vehicle"] +
                               [protocol.get("comparator"), protocol.get("control")]}
        return {k: [v for v in vals if v] for k, vals in terms.items()}

    def _ground_slots(self, run: dict, goal: dict, result: dict) -> None:
        """Check every term the requester named against the files.

        A term the files contain is kept. The same term written differently ("abc 12",
        "ABC12") is read as the files' name, and the reply says so. Anything else is
        ungrounded: the Planner is told to ask, and run_stats stays blocked until the
        requester answers. Nothing is substituted."""
        known, terms = self._known_values(), self._match_terms()
        notes = []
        goal["ungrounded"] = {}
        for slot in goal.get("unclear_slots", []):
            # The request named this, but in a way the Router couldn't copy: ask, don't guess.
            if slot not in goal.get("confirmed_slots", []) and known.get(slot):
                goal["ungrounded"][slot] = {"asked": "(unclear in the request)", "known": known[slot]}
                notes.append(f"The {slot} in the request isn't clear. Ask the requester which they "
                             f"mean (about='slot:{slot}'). Don't assume it's {', '.join(known[slot])}.")
        for slot in goal.get("asked_slots", {}):
            asked = goal["slots"].get(slot)
            options, names = known.get(slot) or [], terms.get(slot) or []
            if not asked or not names or asked in names or slot in goal.get("confirmed_slots", []):
                continue
            same = [o for o in names if _norm(o) == _norm(asked)]
            if not same and slot != "compound":
                # "SA-beta-gal" is the readout "SA-beta-gal fluorescence (C12FDG)"; "DMSO" is the
                # control "0.1% DMSO". A compound must match exactly: "ABC" isn't "ABC-12".
                same = [o for o in names if _norm(asked) in _norm(o) or _norm(o) in _norm(asked)]
            if same:
                goal["slots"][slot] = same[0]
                self._say(run.get("progress_id"), "slot_read_as", asked=asked, name=same[0], slot=slot)
                notes.append(f"The requester wrote {asked!r} for the {slot}; the files call it {same[0]}.")
            else:
                goal["ungrounded"][slot] = {"asked": asked, "known": options}
                notes.append(f"The requester named the {slot} {asked!r}, but the files only have "
                             f"{', '.join(options)}. Ask the requester which they mean "
                             f"(about='slot:{slot}') before analysing. Don't substitute one.")
        self._save("goals", goal)
        if notes:
            result["goal_check"] = " ".join(notes)

    def _after_tool(self, run: dict, goal: dict, name: str, args: dict, result: dict) -> None:
        if args.get("path") and self.drive.owner_of(args["path"]):
            # Which team files this goal's analysis actually read, for "Files not used".
            seen = goal.setdefault("files_read", {})
            seen[args["path"]] = "read" if result.get("ok") else result.get("error", "not read")
            self._save("goals", goal)
        if not result.get("ok"):
            return
        if name in ("parse_xlsx", "extract_protocol_fields"):
            self._ground_slots(run, goal, result)
        if name == "extract_protocol_fields":
            # The protocol fills the slots it states, so they're never asked for.
            for slot in ("compound", "readout", "comparator"):
                if not goal["slots"].get(slot) and result["fields"].get(slot):
                    goal["slots"][slot] = result["fields"][slot]
            self._save("goals", goal)
        if name == "plan_followup_plate":
            meta_rel = result["plan"].replace(".csv", ".json")
            meta = self.drive.read_json(meta_rel)
            meta.update(goal_id=goal["id"], run_id=run["id"], approver=goal["requester"])
            meta["purpose"] = self._plan_purpose(meta)
            self.drive.write_json(meta_rel, meta)
            q = self._new_question(run, goal, to=goal["requester"], cc=[],
                                   about=f"approval:{result['plan']}",
                                   text=f"Approve this {meta['kind']} plate plan ({meta['wells']} wells, "
                                        f"{meta['vehicle_wells']} vehicle wells on the plate)?",
                                   choices=[{"key": "approve", "label": "Approve"},
                                            {"key": "reject", "label": "Reject"}])
            result["approval_question"] = q["id"]

    # ---- questions --------------------------------------------------------------------

    def _new_question(self, run, goal, *, to, cc, about, text, choices) -> dict:
        existing = next((q for q in self.questions(status="open") if q["about"] == about), None)
        if existing:
            return existing
        kind = about.split(":")[0]
        must = {"contradiction": ["exclude", "keep"], "approval": ["approve"]}.get(kind, [])
        options = [c["label"] for c in choices if c["key"] != "other"]
        slot = about.partition(":")[2] if kind == "slot" else None
        message = self.qa.ask(self._person(to), text,
                              {"request_as_written": goal["question"], "about": about,
                               "confirmed_terms": {k: v for k, v in goal["slots"].items()
                                                   if k in TERM_SLOTS and k not in goal.get("ungrounded", {})}},
                              options=options, must_mention=must, renamed=_renamed(goal, slot))
        q = {"id": self._new_id("questions", "q"), "run_id": run["id"], "goal_id": goal["id"],
             "to": to, "cc": cc, "about": about, "text": text, "message": message,
             "choices": choices, "status": "open", "asked": _now()}
        self._save("questions", q)
        self._notify(to, "question", message, q["id"])
        for person in cc:
            self._notify(person, "fyi", self.qa.copy(self._person(person), self._person(to), text,
                                                     renamed=_renamed(goal, about.partition(":")[2])),
                         q["id"])
        return q

    def _create_questions(self, run: dict, goal: dict, args: dict) -> tuple[list[str], list[str]]:
        created, errors = [], []
        items = args.get("questions") or []
        pending_terms = set(goal.get("ungrounded", {}))
        if pending_terms:
            # First settle what the requester meant; other questions would be phrased around
            # terms that may be about to change. They're asked once these are answered.
            held = [i for i in items if str(i.get("about", "")).partition(":")[2] not in pending_terms]
            items = [i for i in items if str(i.get("about", "")).partition(":")[2] in pending_terms]
            if held:
                errors.append("held back until the requester confirms what they meant by: "
                              f"{', '.join(sorted(pending_terms))}. Ask the rest after that")
            for slot in sorted(pending_terms - {str(i.get('about', '')).partition(':')[2] for i in items}):
                items.append({"about": f"slot:{slot}", "text": ""})   # ask even if the Planner didn't
        for item in items:
            about, text = str(item.get("about", "")), str(item.get("text", "")).strip()
            kind, _, ref = about.partition(":")
            if kind == "contradiction":
                c = next((c for c in self.drive.contradictions() if c["id"] == ref), None)
                if c is None:
                    errors.append(f"no contradiction {ref}")
                    continue
                if c["status"] != "open":
                    errors.append(f"{ref} is already resolved")
                    continue
                issues = "; ".join(f"{i['field']} is {i['data_value']}, protocol says {i['protocol_value']}"
                                   for i in c["issues"])
                q = self._new_question(
                    run, goal, to=c["decides"], cc=[o for o in c["owners"] if o != c["decides"]],
                    about=about, text=text or f"Plate {c['plate_id']} in {c['file']}: {issues}. What should we do?",
                    choices=[{"key": k, "label": v} for k, v in c["choices"].items()])
            elif kind == "slot" and ref in TERM_SLOTS:
                # The options are what the files contain, never the Planner's own choices.
                u = goal.get("ungrounded", {}).get(ref) or {
                    "asked": goal["slots"].get(ref) or "(not given)", "known": self._known_values().get(ref, [])}
                choices = [{"key": f"c{i + 1}", "label": k, "value": k} for i, k in enumerate(u["known"])]
                choices.append({"key": "other", "label": "Something else (type it)", "value": None})
                q = self._new_question(run, goal, to=goal["requester"], cc=[], about=about,
                                       text=text or (f"You named the {ref} {u['asked']!r}, but the files "
                                                     f"only have {', '.join(u['known'])}. Which did you mean?"
                                                     if u["known"] else f"Which {ref} should the analysis use?"),
                                       choices=choices)
            elif kind == "slot" and ref in REQUIRED_SLOTS:
                choices = [c for c in item.get("choices") or [] if _valid_slot(ref, c.get("value"))]
                if not choices:
                    errors.append(f"slot:{ref} needs at least one choice with a valid value")
                    continue
                # The Q&A agent labels the buttons from the values; the Planner's own labels
                # never reach the scientist. The fixed format is only the fallback.
                values = [c["value"] for c in choices[:3]]
                labels = self.qa.labels(self._person(goal["requester"]), text, values,
                                        [_slot_text(ref, v).capitalize() for v in values])
                choices = [{"key": f"c{i + 1}", "label": label, "value": v}
                           for i, (label, v) in enumerate(zip(labels, values))]
                choices.append({"key": "other", "label": "Something else (type it)", "value": None})
                q = self._new_question(run, goal, to=goal["requester"], cc=[], about=about,
                                       text=text or f"What {ref} should we use?", choices=choices)
            else:
                errors.append(f"can't ask about {about!r}: use contradiction:<id> or slot:<name>")
                continue
            created.append(q["id"])
        return created, errors

    # ---- review and findings ------------------------------------------------------------

    def _evidence(self, goal: dict) -> dict:
        claims = self.drive.read_json(files.NOTEBOOK_CLAIMS, {}) or {}
        sops = {f["path"]: self.drive.path(f["path"]).read_text() for f in self.drive.files()
                if f["path"].startswith("sops/")}
        return {"goal_slots": goal["slots"], "stats": self.drive.read_json(STATS), "sops": sops,
                "facts": self.drive.facts(), "contradictions": self.drive.contradictions(),
                "notebook_claims": {k: v for k, v in claims.items() if k != "code"}}

    def _review_and_finish(self, run: dict, goal: dict, draft: dict) -> dict | None:
        review = self.reviewer.run(draft, self._evidence(goal), self.drive.read_json,
                                   compound=goal["slots"].get("compound"))
        run["last_review"] = review
        self._trace(run, {"event": "review", **review})
        if not review["pass"]:
            run["revisions"] += 1
            if run["revisions"] <= self.max_revisions:
                return None
        unresolved = [] if review["pass"] else review["problems"]
        self._say(run.get("progress_id"), "writing")

        previous = goal["history"][-1] if goal["history"] else None
        goal["history"].append({"run": run["id"], "verdict": draft.get("verdict"),
                                "numbers": draft.get("numbers", []), "at": _now()})
        goal.update(status="answered", last_draft=draft, last_unresolved=unresolved,
                    previous=previous, last_run=run["id"])
        self._save("goals", goal)
        md = self._render(goal)
        self.drive.write_json(f"findings/drafts/{run['id']}.json", draft)

        for n in draft.get("notes_for_owners") or []:
            owner = n.get("owner")
            if owner in self.drive.people:
                rel = f"findings/notes/{str(n.get('file', 'general')).split('/')[-1]}.md"
                p = self.drive.path(rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a") as fh:
                    fh.write(f"## {run['id']}, for {self._person(owner)['name']}\n\n{n.get('note', '')}\n\n")
                self._notify(owner, "note", self.qa.note(self._person(owner), str(n.get("file")),
                                                         n.get("note", ""), renamed=_renamed(goal)), rel)
        self._notify(goal["requester"], "report",
                     self.qa.report(self._person(goal["requester"]), md, renamed=_renamed(goal)),
                     "findings/findings.md")
        status = "done" if review["pass"] else "done_with_problems"
        self._say(run.get("progress_id"), "done", state="done", verdict=draft.get("verdict", "finished"))
        return self._finish(run, status, "; ".join(unresolved))

    def _rerender(self, goal_id: str) -> None:
        goal = self._load("goals", goal_id)
        if goal and goal.get("last_draft"):
            self._render(goal)

    def _render(self, goal: dict) -> str:
        """findings.md is built from the approved draft and the drive. No free LLM text goes
        in except the reviewed summary, next step and notes."""
        d = goal["last_draft"]
        people = self.drive.people
        name = lambda pid: people.get(pid, {}).get("name", pid)       # noqa: E731
        th = goal["slots"].get("threshold")
        sl = goal["slots"]
        title = " vs ".join(x for x in (sl.get("compound"), sl.get("comparator")) if x) or goal["question"]
        lines = [f"# Findings: {title}" + (f" · readout: {sl['readout']}" if sl.get("readout") else ""), "",
                 f"Asked as: \"{goal['question']}\"", "",
                 f"Requested by {name(goal['requester'])} · goal {goal['id']} · run {goal['last_run']} · "
                 f"{dt.date.today().isoformat()}", "",
                 "## Verdict", "", f"**{d['verdict']}**" +
                 (f" (threshold: {_slot_text('threshold', th)}, confirmed by the requester)" if th else ""), "",
                 "## Evidence [raw]", "", "| What | Value | Source |", "|---|---|---|"]
        for n in d.get("numbers", []):
            lines.append(f"| {n['label']} | {n['value']} | `{n['source']}` → `{n['key']}` |")
        lines += ["", "## Decisions the team made", ""]
        for f in self.drive.facts():
            lines.append(f"- {f['statement']} (confirmed by {name(f['confirmed_by'])}, {f['id']})")
        if not self.drive.facts():
            lines.append("- None needed.")
        lines += ["", "## Interpretation [inferred]", "", d["summary"], ""]
        prev = goal.get("previous")
        if prev:
            lines += ["## What changed since the last run", "",
                      f"- Verdict: {prev['verdict']} → {d['verdict']}"]
            before = {n["label"]: n["value"] for n in prev.get("numbers", [])}
            for n in d.get("numbers", []):
                if n["label"] in before and before[n["label"]] != n["value"]:
                    lines.append(f"- {n['label']}: {before[n['label']]} → {n['value']}")
            lines.append("")
        lines += ["## Next step", "", d.get("next_step", "")]
        if d.get("plan"):
            meta = self.drive.read_json(d["plan"].replace(".csv", ".json"), {}) or {}
            lines.append(f"\nPlate plan: `{d['plan']}` ({meta.get('status', 'unknown')})")
        if d.get("notes_for_owners"):
            lines += ["", "## Notes for file owners", ""]
            lines += [f"- {name(n.get('owner'))} on `{n.get('file')}`: {n.get('note')}"
                      for n in d["notes_for_owners"]]
        unused = []
        for f in self.drive.files():
            status = goal.get("files_read", {}).get(f["path"])
            if f["path"].startswith("sops/") or status == "read":
                continue
            unused.append(f"- `{f['path']}` ({name(f['owner'])}): " +
                          (status if status else "not read in this analysis"))
        if unused:
            lines += ["", "## Files not used", ""] + unused
        if goal.get("last_unresolved"):
            lines += ["", "## Review problems not resolved", ""]
            lines += [f"- {p}" for p in goal["last_unresolved"]]
        md = "\n".join(lines) + "\n"
        self.drive.path("findings/history").mkdir(parents=True, exist_ok=True)
        self.drive.path("findings/findings.md").write_text(md)
        self.drive.path(f"findings/history/{goal['last_run']}.md").write_text(md)
        return md


TERM_SLOTS = ("compound", "readout", "comparator")
QUESTION_WORDS = ("why", "what", "how", "is", "are", "does", "do", "did", "can", "will", "which",
                  "who", "when", "where", "should", "could", "would", "was", "were")


def _looks_like_question(text: str) -> bool:
    words = text.strip().lower().split()
    return "?" in text or bool(words and words[0].strip(",.") in QUESTION_WORDS)


def _renamed(goal: dict, except_slot: str | None = None) -> dict[str, str]:
    """Terms the requester wrote that now have a different confirmed name, old -> new.
    A question about that very term may quote it, so it can be left out."""
    out = {}
    for slot, asked in (goal.get("asked_slots") or {}).items():
        now = (goal.get("slots") or {}).get(slot)
        if slot != except_slot and isinstance(now, str) and slot not in (goal.get("ungrounded") or {}) \
                and _norm(asked) != _norm(now):
            out[asked] = now
    return out


def _norm(name: str) -> str:
    """'abc 12', 'ABC12' and 'ABC-12' are the same compound name."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _valid_slot(slot: str, value: Any) -> bool:
    if slot == "threshold":
        return (isinstance(value, dict)
                and isinstance(value.get("min_drop"), (int, float))
                and isinstance(value.get("max_p"), (int, float))
                and 0 < value["min_drop"] < 1 and 0 < value["max_p"] < 1)
    return isinstance(value, str) and bool(value.strip())


def _slot_text(slot: str, value: Any) -> str:
    if slot == "threshold" and isinstance(value, dict):
        return f"drop of at least {value['min_drop']:.0%} with p < {value['max_p']}"
    return str(value)
