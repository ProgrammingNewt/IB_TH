"""The harness: goals, runs, the Planner's tool loop, questions, approvals and findings.

Everything a run needs to continue is stored in the drive:

    goals/g-*.json       what was asked, by whom, and the goal's slots
    runs/r-*.json        the Planner's full conversation, so a paused run can resume
    questions/q-*.json   questions to scientists, their choices and answers
    messages/<id>.json   each scientist's inbox
    trace/r-*.jsonl      every model call and tool call in a run
    findings/            findings.md, its history, notes for file owners, plate plans

Entry points, called by the UI or a script: `message`, `answer`, `upload`.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from harness.agents import agents
from harness.agents.prompts import PLANNER
from harness.drive.store import Drive
from harness.gateway.gateway import Gateway, GatewayError, TokenBudgetExceeded, jsonl_tracer
from harness.state import REQUIRED_SLOTS, RunState
from harness.tools import files, registry
from harness.tools.analysis import STATS

CONTROL_TOOLS = [
    {"type": "function", "function": {
        "name": "ask_scientists",
        "description": "Ask scientists questions. The run pauses until every question is answered.",
        "parameters": {"type": "object", "required": ["questions"], "properties": {
            "questions": {"type": "array", "items": {"type": "object", "required": ["about", "text"],
                "properties": {
                    "about": {"type": "string",
                              "description": "contradiction:<id> or slot:threshold"},
                    "to": {"type": "string", "description": "person id; set by the harness for contradictions"},
                    "text": {"type": "string", "description": "the question"},
                    "choices": {"type": "array", "items": {"type": "object",
                        "properties": {"label": {"type": "string"}, "value": {"type": "object"}}}},
                }}}}}}},
    {"type": "function", "function": {
        "name": "submit_conclusion",
        "description": "Submit the conclusion for review. Every number must cite its source.",
        "parameters": {"type": "object",
            "required": ["verdict", "summary", "numbers", "next_step"],
            "properties": {
                "verdict": {"type": "string", "enum": ["hit", "not a hit", "inconclusive"]},
                "summary": {"type": "string"},
                "numbers": {"type": "array", "items": {"type": "object",
                    "required": ["label", "value", "source", "key"],
                    "properties": {"label": {"type": "string"}, "value": {"type": "number"},
                                   "source": {"type": "string", "description": "e.g. derived/stats.json"},
                                   "key": {"type": "string", "description": "e.g. drop"}}}},
                "notes_for_owners": {"type": "array", "items": {"type": "object",
                    "properties": {"owner": {"type": "string"}, "file": {"type": "string"},
                                   "note": {"type": "string"}}}},
                "next_step": {"type": "string"},
                "plan": {"type": "string", "description": "plan file from plan_followup_plate, if any"},
            }}}},
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Harness:
    def __init__(self, drive: Drive, gateway: Gateway, max_turns: int = 40,
                 max_revisions: int = 2, tool_budget: int = 25):
        self.drive, self.gw = drive, gateway
        self.max_turns, self.max_revisions, self.tool_budget = max_turns, max_revisions, tool_budget

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
        if person not in self.drive.people:
            return
        box = self.inbox(person)
        box.append({"at": _now(), "kind": kind, "text": text, "ref": ref})
        self.drive.write_json(f"messages/{person}.json", box)

    def _person(self, pid: str) -> dict:
        return {"id": pid, **self.drive.people.get(pid, {"name": pid})}

    def _trace(self, run: dict, event: dict) -> None:
        jsonl_tracer(self.drive.path(f"trace/{run['id']}.jsonl"))(event)

    # ---- entry points -------------------------------------------------------------

    def message(self, sender: str, text: str) -> dict:
        """A scientist typed something. The Router decides what to do with it."""
        if sender not in self.drive.people:
            raise ValueError(f"unknown person {sender!r}")
        routed = agents.route(self.gw, self._person(sender), text)
        if routed["intent"] == "question_about_findings":
            findings = self.drive.path("findings/findings.md")
            reply = agents.qa_answer(self.gw, self._person(sender), text,
                                     findings.read_text() if findings.exists() else "")
            self._notify(sender, "answer", reply)
            return {"intent": routed["intent"], "reply": reply}
        if routed["intent"] != "new_analysis":
            reply = "I can analyse an experiment's data on this drive, or answer questions about findings."
            self._notify(sender, "answer", reply)
            return {"intent": routed["intent"], "reply": reply}

        goal = {"id": self._new_id("goals", "g"), "requester": sender, "question": text,
                "slots": routed["slots"], "status": "open", "runs": [], "history": [],
                "created": _now()}
        self._save("goals", goal)
        return {"intent": "new_analysis", "goal": goal["id"],
                **self._start_run(goal, f"{self._person(sender)['name']} asked: {text}")}

    def upload(self, src, owner: str, description: str) -> dict:
        """A scientist added a file. Every goal that already has a run is re-evaluated."""
        rel = self.drive.upload(src, owner, description)
        runs = []
        for goal in self.goals():
            if goal["runs"]:
                runs.append(self._start_run(
                    goal, f"New file uploaded by {self._person(owner)['name']}: {rel}. "
                          "Re-evaluate the goal with it. Reuse facts the team already confirmed."))
        return {"uploaded": rel, "runs": runs}

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
            if choice == "other":
                parsed = agents.qa_parse_other(self.gw, q, text or "")
                value = parsed["value"]
                if not _valid_slot(ref, value):
                    self._notify(by, "question", "I couldn't turn that into a threshold. Could you "
                                 "give a minimum drop and a p-value, e.g. '30% and 0.05'?", qid)
                    return {"recorded_as": "nothing", "reason": "couldn't parse the answer"}
            goal = self._load("goals", q["goal_id"])
            goal["slots"][ref] = value
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
        return {"recorded_as": "fact" if "fact" in recorded else "decision", **recorded,
                **self._maybe_resume(q["run_id"])}

    # ---- runs -----------------------------------------------------------------------

    def _start_run(self, goal: dict, trigger: str) -> dict:
        run = {"id": self._new_id("runs", "r"), "goal_id": goal["id"], "trigger": trigger,
               "status": "running", "tool_calls": 0, "turns": 0, "revisions": 0, "nudges": 0,
               "pending": None, "started": _now(),
               "messages": [{"role": "system", "content": PLANNER},
                            {"role": "user", "content": self._context(goal, trigger)}]}
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
        run["messages"].append({"role": "tool", "tool_call_id": run["pending"]["tool_call_id"],
                                "content": json.dumps({"ok": True, "answers": answers})})
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

    def _overrides(self) -> dict:
        extractor = agents.llm_protocol_extractor(self.gw)
        return {"extract_protocol_fields":
                lambda drive, path: files.extract_protocol_fields(drive, path, extractor=extractor)}

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
        goal = self._load("goals", run["goal_id"])
        self.gw.tokens_used = run.get("tokens_used", 0)     # each run has its own token budget
        self.gw.tracer = jsonl_tracer(self.drive.path(f"trace/{run['id']}.jsonl"))
        state = RunState(goal=goal["slots"], budget=self.tool_budget, tool_calls=run["tool_calls"])
        overrides = self._overrides()
        tools = registry.openai_tools() + CONTROL_TOOLS

        while run["turns"] < self.max_turns:
            run["turns"] += 1
            try:
                c = self.gw.complete("planner", run["messages"], tools=tools)
            except (GatewayError, TokenBudgetExceeded) as exc:
                self._notify(goal["requester"], "status", f"The analysis stopped: {exc}", run["id"])
                return self._finish(run, "failed", str(exc))

            run["messages"].append({
                "role": "assistant", "content": c.content or "",
                **({"tool_calls": [{"id": t["id"], "type": "function",
                                    "function": {"name": t["name"], "arguments": t["arguments"]}}
                                   for t in c.tool_calls]} if c.tool_calls else {})})
            if not c.tool_calls:
                run["nudges"] += 1
                if run["nudges"] > 3:
                    return self._finish(run, "failed", "the Planner stopped calling tools")
                run["messages"].append({"role": "user", "content":
                    "Call a tool, ask_scientists, or submit_conclusion."})
                continue

            ask = submit = None
            for t in c.tool_calls:
                try:
                    args = json.loads(t["arguments"] or "{}")
                except json.JSONDecodeError:
                    self._tool_reply(run, t["id"], {"ok": False, "error": "arguments weren't valid JSON"})
                    continue
                if t["name"] == "ask_scientists":
                    ask = (t, args)
                elif t["name"] == "submit_conclusion":
                    submit = (t, args)
                else:
                    state.goal = goal["slots"]
                    result = registry.call(self.drive, state, t["name"], args, overrides)
                    run["tool_calls"] = state.tool_calls
                    self._after_tool(run, goal, t["name"], result)
                    self._trace(run, {"event": "tool", "tool": t["name"], "args": args,
                                      "ok": result.get("ok"), "blocked": result.get("blocked"),
                                      "error": result.get("error")})
                    self._tool_reply(run, t["id"], result)

            if ask:
                t, args = ask
                created, errors = self._create_questions(run, goal, args)
                if submit:
                    self._tool_reply(run, submit[0]["id"], {"ok": False, "error":
                        "not reviewed: answer-pending questions come first"})
                if not created:
                    self._tool_reply(run, t["id"], {"ok": False, "errors": errors})
                    self._save_run(run)
                    continue
                run.update(status="waiting", pending={"tool_call_id": t["id"], "questions": created})
                if errors:
                    run["pending"]["errors"] = errors
                self._save_run(run)
                self._trace(run, {"event": "waiting", "questions": created, "errors": errors})
                return {"run": run["id"], "status": "waiting", "questions": created}

            if submit:
                t, draft = submit
                done = self._review_and_finish(run, goal, draft)
                if done:
                    return done
                self._tool_reply(run, t["id"], {"ok": False, "review": run["last_review"],
                                                "revisions_left": self.max_revisions - run["revisions"]})
            self._save_run(run)

        return self._finish(run, "failed", f"turn limit of {self.max_turns} reached")

    def _tool_reply(self, run: dict, call_id: str, result: dict) -> None:
        run["messages"].append({"role": "tool", "tool_call_id": call_id,
                                "content": json.dumps(result, default=str)})

    def _after_tool(self, run: dict, goal: dict, name: str, result: dict) -> None:
        if not result.get("ok"):
            return
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
        message = agents.qa_ask(self.gw, self._person(to), text,
                                {"goal": goal["question"], "about": about})
        q = {"id": self._new_id("questions", "q"), "run_id": run["id"], "goal_id": goal["id"],
             "to": to, "cc": cc, "about": about, "text": text, "message": message,
             "choices": choices, "status": "open", "asked": _now()}
        self._save("questions", q)
        self._notify(to, "question", message, q["id"])
        for person in cc:
            self._notify(person, "fyi", f"{self._person(to)['name']} was asked: {text}", q["id"])
        return q

    def _create_questions(self, run: dict, goal: dict, args: dict) -> tuple[list[str], list[str]]:
        created, errors = [], []
        for item in args.get("questions") or []:
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
            elif kind == "slot" and ref in REQUIRED_SLOTS:
                choices = [c for c in item.get("choices") or [] if _valid_slot(ref, c.get("value"))]
                if not choices:
                    errors.append(f"slot:{ref} needs at least one choice with a valid value")
                    continue
                choices = [{"key": f"c{i + 1}", "label": c.get("label") or _slot_text(ref, c["value"]),
                            "value": c["value"]} for i, c in enumerate(choices[:3])]
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
        return {"goal_slots": goal["slots"], "stats": self.drive.read_json(STATS),
                "facts": self.drive.facts(), "contradictions": self.drive.contradictions(),
                "notebook_claims": {k: v for k, v in claims.items() if k != "code"}}

    def _review_and_finish(self, run: dict, goal: dict, draft: dict) -> dict | None:
        try:
            review = agents.review(self.gw, draft, self._evidence(goal), self.drive.read_json,
                                   compound=goal["slots"].get("compound"))
        except (GatewayError, agents.AgentReplyError) as exc:
            review = {"pass": False, "problems": [f"review couldn't run: {exc}"], "by": "error"}
        run["last_review"] = review
        self._trace(run, {"event": "review", **review})
        if not review["pass"]:
            run["revisions"] += 1
            if run["revisions"] <= self.max_revisions:
                return None
        unresolved = [] if review["pass"] else review["problems"]

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
                self._notify(owner, "note", f"There's a note on {n.get('file')}: {n.get('note', '')}", rel)
        self._notify(goal["requester"], "report",
                     agents.qa_report(self.gw, self._person(goal["requester"]), md), "findings/findings.md")
        status = "done" if review["pass"] else "done_with_problems"
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
        lines = [f"# Findings: {goal['question']}", "",
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
        if goal.get("last_unresolved"):
            lines += ["", "## Review problems not resolved", ""]
            lines += [f"- {p}" for p in goal["last_unresolved"]]
        md = "\n".join(lines) + "\n"
        self.drive.path("findings/history").mkdir(parents=True, exist_ok=True)
        self.drive.path("findings/findings.md").write_text(md)
        self.drive.path(f"findings/history/{goal['last_run']}.md").write_text(md)
        return md


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
