"""Run eval cases against the real harness and real models, each on its own fresh drive.

    python -m evals.runner evals/cases/core.json --out runs/evals/core
    python -m evals.runner evals/cases/core.json --out runs/evals/core --only planted

A case is a JSON object:

    {
      "name": "typo_compound",
      "scenario": "planted_batch_issue",          # or "clean"; seed is optional
      "request": {"who": "A", "text": "is acb 12 a hit?"},
      "extra_files": [{"name": "notes.txt", "owner": "C", "description": "...", "content": "..."}],
      "refuse_provider_1": false,                  # provider 1 refuses everything
      "answers": {                                 # how the scientists answer; defaults below
        "compound": "first",                       # first option, or "other:<text>"
        "readout": "first", "comparator": "first",
        "threshold": "0.3/0.05",                   # a choice with this value, else typed
        "contradiction": "exclude",                # exclude | keep | other:<text>
        "approval": "approve",                     # approve | reject
        "wrong_person_first": ["contradiction"]    # someone else answers first (must be a comment)
      },
      "chat": [{"who": "A", "text": "why is Ben being asked?", "when": "waiting"}],   # or "done"
      "upload_rerun": true,                        # run 2: Ben uploads the re-run file
      "expect": { ... see CHECKS ... }
    }

Results go to <out>/<case>/result.json (everything recorded) and <out>/summary.md.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

from datagen.scenarios import DEFAULT_SEED, SCENARIOS
from datagen.writers import write_scenario
from harness.drive.store import Drive
from harness.gateway.fakes import RefusingClient
from harness.gateway.gateway import Gateway, _openai_client, load_config
from harness.harness import Harness

DEFAULT_ANSWERS = {"compound": "first", "readout": "first", "comparator": "first",
                   "threshold": "0.3/0.05", "contradiction": "exclude", "approval": "approve"}


def _norm(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


# ---- running a case ------------------------------------------------------------------

def _gateway(refuse_provider_1: bool) -> Gateway:
    def factory(p):
        return RefusingClient() if refuse_provider_1 and p.name == "model_provider_1" else _openai_client(p)
    return Gateway(load_config(), client_factory=factory)


def _choice_for(q: dict, action: str) -> tuple[str, str | None]:
    """Turn an answer policy into (choice key, typed text)."""
    keys = [c["key"] for c in q["choices"]]
    if action.startswith("other:"):
        return "other", action[len("other:"):]
    if action == "first":
        return next(k for k in keys if k != "other"), None
    if "/" in action and q["about"] == "slot:threshold":
        drop, p = (float(x) for x in action.split("/"))
        for c in q["choices"]:
            v = c.get("value") or {}
            if abs(v.get("min_drop", -1) - drop) < 1e-9 and abs(v.get("max_p", -1) - p) < 1e-9:
                return c["key"], None
        return "other", f"a {drop:.0%} drop with p below {p}"
    if action in keys:
        return action, None
    return "other", action


def _answer_open(h: Harness, policy: dict, log: list, rounds: int = 8) -> None:
    wrong_first = set(policy.get("wrong_person_first", []))
    for _ in range(rounds):
        open_qs = h.questions(status="open")
        if not open_qs:
            return
        for q in open_qs:
            kind, _, ref = q["about"].partition(":")
            key = ref if kind == "slot" else kind
            action = policy.get(key, DEFAULT_ANSWERS.get(key, "first"))
            if action == "skip":
                continue
            choice, text = _choice_for(q, action)
            if key in wrong_first:
                other = next(p for p in h.drive.people if p != q["to"])
                res = h.answer(q["id"], other, choice, text)
                log.append({"question": q["id"], "about": q["about"], "by": other, "choice": choice,
                            "text": text, "result": res, "wrong_person": True})
                wrong_first.discard(key)
            res = h.answer(q["id"], q["to"], choice, text)
            log.append({"question": q["id"], "about": q["about"], "by": q["to"], "choice": choice,
                        "text": text, "result": {k: v for k, v in res.items() if k != "answers"}})
        if all(x["status"] == "open" for x in h.questions(status="open")) and \
                {x["id"] for x in h.questions(status="open")} == {x["id"] for x in open_qs}:
            return                                           # nothing moved: stop, don't loop


def _chat(h: Harness, case: dict, when: str, log: list) -> None:
    for c in case.get("chat", []):
        if c.get("when", "waiting") != when:
            continue
        pid = h.receive(c["who"], c["text"])
        try:
            out = h.chat(c["who"], c["text"], pid)
        except Exception as exc:                                       # noqa: BLE001
            out = {"error": f"{type(exc).__name__}: {exc}"}
        log.append({"when": when, "who": c["who"], "text": c["text"], "intent": out.get("intent"),
                    "reply": out.get("reply"), "error": out.get("error")})


def run_case(case: dict, out: Path) -> dict:
    """Run one case on a fresh drive under `out/<name>`. Returns everything recorded."""
    root = out / case["name"]
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    scenario, seed = case.get("scenario", "planted_batch_issue"), case.get("seed", DEFAULT_SEED)
    s = SCENARIOS[scenario](seed)
    drive_dir = write_scenario(s, root, drive_name="drive")
    drive = Drive(drive_dir)
    for f in case.get("extra_files", []):
        src = root / f["name"]
        src.write_text(f["content"])
        drive.upload(src, f["owner"], f.get("description", f["name"]))
    h = Harness(Drive(drive_dir), _gateway(case.get("refuse_provider_1", False)))
    policy = {**DEFAULT_ANSWERS, **case.get("answers", {})}

    record = {"name": case["name"], "case": case, "started": time.time(), "errors": [],
              "answers": [], "chat": [], "first_round": []}
    try:
        req = case["request"]
        first = h.message(req["who"], req["text"])
        record["first_response"] = {k: v for k, v in first.items() if k != "answers"}
        record["first_round"] = [{"to": q["to"], "about": q["about"], "message": q["message"],
                                  "buttons": [c["label"] for c in q["choices"]]}
                                 for q in h.questions(status="open")]
        _chat(h, case, "waiting", record["chat"])
        _answer_open(h, policy, record["answers"])
        _chat(h, case, "done", record["chat"])
        if case.get("upload_rerun"):
            incoming = root / "drive.incoming" / "readings_rerun.xlsx"
            if incoming.exists():
                record["run2"] = {k: v for k, v in h.upload(incoming, "B", "Re-run at 72 h with vehicle").items()}
                _answer_open(h, policy, record["answers"])
    except Exception as exc:                                           # noqa: BLE001
        record["errors"].append(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")

    record["seconds"] = round(time.time() - record.pop("started"), 1)
    record.update(_collect(h, drive_dir))
    record["checks"] = check(record, case.get("expect", {}))
    record["passed"] = all(c["ok"] for c in record["checks"])
    (root / "result.json").write_text(json.dumps(record, indent=2, default=str))
    return record


def _collect(h: Harness, drive_dir: Path) -> dict:
    d = h.drive
    runs = [h._load("runs", r) for r in h._ids("runs")]
    trace = []
    for name in ["requests"] + [r["id"] for r in runs]:
        p = d.path(f"trace/{name}.jsonl")
        trace += [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
    findings = d.path("findings/findings.md")
    drafts = sorted(d.path("findings/drafts").glob("*.json")) if d.path("findings/drafts").exists() else []
    return {
        "goals": [{k: g.get(k) for k in ("id", "question", "slots", "asked_slots", "status", "ungrounded")}
                  for g in h.goals()],
        "runs": [{k: r.get(k) for k in ("id", "status", "note", "tool_calls", "turns", "revisions",
                                         "tokens_used")} for r in runs],
        "questions": [{k: q.get(k) for k in ("id", "to", "cc", "about", "status", "message")} |
                      {"buttons": [c["label"] for c in q["choices"]], "answer": (q.get("answer") or {})}
                      for q in h.questions()],
        "inbox": {p: h.inbox(p) for p in d.people},
        "threads": [{"by": t["by"], "text": t["text"], "state": t["state"],
                     "steps": [s["text"] for s in t["steps"]]} for t in h.threads()],
        "facts": d.facts(),
        "contradictions": d.contradictions(),
        "stats": d.read_json("derived/stats.json"),
        "last_draft": json.loads(drafts[-1].read_text()) if drafts else None,
        "findings": findings.read_text() if findings.exists() else None,
        "tools": [{"tool": e["tool"], "ok": e["ok"], "blocked": e.get("blocked"), "error": e.get("error")}
                  for e in trace if e.get("event") == "tool"],
        "providers": sorted({f"{e.get('label')}:{e.get('outcome')}" for e in trace if e.get("agent")}),
        "fell_back": any(e.get("fell_back") for e in trace) or bool(h.gw.benched),
        "benched": dict(h.gw.benched),
        "refusals": sum(1 for e in trace if e.get("outcome") == "refused"),
    }


# ---- checks ------------------------------------------------------------------------------

def _all_messages(rec: dict) -> list[tuple[str, str, str]]:
    return [(p, m["kind"], m["text"]) for p, box in rec["inbox"].items() for m in box]


def check(rec: dict, expect: dict) -> list[dict]:
    """Each expectation becomes {check, ok, detail}. Unknown keys are reported as failures."""
    out = []

    def add(name, ok, detail=""):
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    add("no errors", not rec["errors"], "; ".join(e.splitlines()[0] for e in rec["errors"]))
    asked = [q["about"] for q in rec["questions"]]
    first = [q["about"] for q in rec["first_round"]]
    verdict = (rec.get("last_draft") or {}).get("verdict")
    for key, want in expect.items():
        if key == "verdict":
            add(f"verdict is {want!r}", verdict == want, f"got {verdict!r}")
        elif key == "run_status":
            got = [r["status"] for r in rec["runs"]]
            add(f"every run ends {want!r}", got and all(s == want for s in got), f"got {got}")
        elif key == "asked":
            for a in want:
                add(f"asked about {a}", any(x.startswith(a) for x in asked), f"asked {asked}")
        elif key == "not_asked":
            for a in want:
                add(f"never asked about {a}", not any(x.startswith(a) for x in asked), f"asked {asked}")
        elif key == "first_round":
            add(f"first round is exactly {sorted(want)}", sorted(first) == sorted(want), f"got {sorted(first)}")
        elif key == "first_round_includes":
            for a in want:
                add(f"first round asks about {a}", any(x.startswith(a) for x in first), f"got {first}")
        elif key == "asked_to":
            for about, person in want.items():
                q = next((q for q in rec["questions"] if q["about"].startswith(about)), None)
                add(f"{about} goes to {person}", q and q["to"] == person, f"got {q and q['to']}")
        elif key == "forbidden_text":
            for t in want:
                hits = [f"{p}/{k}" for p, k, txt in _all_messages(rec)
                        if _norm(t) in _norm(txt) and k != "question"]
                in_findings = rec.get("findings") and _norm(t) in _norm(rec["findings"].split("Asked as:")[0] +
                                                                         rec["findings"].split("\n", 3)[-1])
                add(f"{t!r} never appears outside the clarifying question", not hits and not in_findings,
                    f"found in {hits}{' and findings' if in_findings else ''}")
        elif key == "findings_contain":
            for t in want:
                add(f"findings mention {t!r}", rec.get("findings") and t.lower() in rec["findings"].lower(), "")
        elif key == "findings_exclude":
            for t in want:
                add(f"findings don't say {t!r}", not (rec.get("findings") and re.search(t, rec["findings"], re.I)),
                    "")
        elif key == "plates_used":
            got = (rec.get("stats") or {}).get("plates_used")
            add(f"stats use {want}", got == want, f"got {got}")
        elif key == "fell_back":
            add(f"fell back to another provider: {want}", rec["fell_back"] == want,
                f"providers {rec['providers']}")
        elif key == "max_goals":
            add(f"at most {want} goal(s)", len(rec["goals"]) <= want, f"got {len(rec['goals'])}")
        elif key == "chat_reply_contains":
            for i, words in want.items():
                reply = (rec["chat"][int(i)].get("reply") or "") if int(i) < len(rec["chat"]) else ""
                for w in words:
                    add(f"chat {i} reply mentions {w!r}", w.lower() in reply.lower(), reply[:200])
        elif key == "chat_intent":
            for i, intent in want.items():
                got = rec["chat"][int(i)].get("intent") if int(i) < len(rec["chat"]) else None
                add(f"chat {i} routed as {intent}", got == intent, f"got {got}")
        else:
            add(f"unknown expectation {key!r}", False, "fix the case file")
    return out


# ---- CLI ---------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="*", help="run only these case names")
    args = ap.parse_args(argv)
    cases = json.loads(args.cases.read_text())
    if args.only:
        cases = [c for c in cases if c["name"] in args.only]
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases:
        print(f"running {case['name']} ...", flush=True)
        rec = run_case(case, args.out)
        failed = [c for c in rec["checks"] if not c["ok"]]
        print(f"  {'PASS' if rec['passed'] else 'FAIL'} in {rec['seconds']}s"
              + "".join(f"\n    x {c['check']}: {c['detail'][:200]}" for c in failed), flush=True)
        rows.append((case["name"], rec["passed"], len(rec["checks"]) - len(failed), len(rec["checks"]),
                     rec["seconds"], sum(r.get("tokens_used") or 0 for r in rec["runs"])))
    lines = ["| case | result | checks | seconds | tokens |", "|---|---|---|---|---|"]
    lines += [f"| {n} | {'pass' if ok else 'FAIL'} | {a}/{b} | {s} | {t:,} |" for n, ok, a, b, s, t in rows]
    summary = args.out / "summary.md"
    summary.write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    return 0 if all(r[1] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
