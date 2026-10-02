"""One live run of the planted scenario with real models and scripted scientists.

Usage: python scripts/live_planted.py runs/live   (needs keys in .env)
"""
import json, shutil, sys, time
from pathlib import Path
from datagen.__main__ import main as gen
from harness.drive.store import Drive
from harness.gateway.gateway import Gateway
from harness.harness import Harness

out = Path(sys.argv[1]); shutil.rmtree(out, ignore_errors=True)
gen(["--out", str(out)])
h = Harness(Drive(out / "planted_batch_issue"), Gateway())

def scientists_answer(max_rounds=6):
    """Scripted answers: B excludes P2, A picks 30%/0.05, A approves plans."""
    for _ in range(max_rounds):
        open_qs = h.questions(status="open")
        if not open_qs:
            return None
        res = None
        for q in open_qs:
            kind = q["about"].split(":")[0]
            if kind == "contradiction":
                res = h.answer(q["id"], q["to"], "exclude")
            elif kind == "slot":
                c = next((c for c in q["choices"] if c.get("value") == {"min_drop": 0.3, "max_p": 0.05}), None)
                res = h.answer(q["id"], q["to"], c["key"]) if c else h.answer(q["id"], q["to"], "other", "30% drop and p below 0.05")
            elif kind == "approval":
                res = h.answer(q["id"], q["to"], "approve")
            print(f"  answered {q['id']} ({q['about']}) as {q['to']}: {q['message'][:110]!r}")
            print("   ->", {k: v for k, v in res.items() if k in ("recorded_as", "status", "still_open", "note", "run")})
    return res

t0 = time.time()
r = h.message("A", "Is ABC-12 a hit?")
print("RUN 1 start ->", r)
scientists_answer()
run1 = h._load("runs", h.goals()[0]["runs"][0])
print("RUN 1:", run1["status"], run1.get("note", ""), "| tool calls", run1["tool_calls"], "| turns", run1["turns"], "| tokens", run1.get("tokens_used"))
print("--- findings ---\n" + (h.drive.path("findings/findings.md").read_text() if h.drive.path("findings/findings.md").exists() else "(none)"))
up = h.upload(out / "planted_batch_issue.incoming" / "readings_rerun.xlsx", "B", "Re-run at 72 h with vehicle on the plate")
print("RUN 2 ->", up)
scientists_answer()
g = h.goals()[0]
run2 = h._load("runs", g["runs"][-1])
print("RUN 2:", run2["status"], run2.get("note", ""), "| tool calls", run2["tool_calls"], "| turns", run2["turns"], "| tokens", run2.get("tokens_used"))
print("questions total:", len(h.questions()))
print("--- findings ---\n" + h.drive.path("findings/findings.md").read_text())
print("inbox A:", [m["kind"] for m in h.inbox("A")], "B:", [m["kind"] for m in h.inbox("B")], "C:", [m["kind"] for m in h.inbox("C")])
print(f"elapsed {time.time()-t0:.0f}s")
