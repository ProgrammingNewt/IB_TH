"""Lab tools: follow-up plate plans, SOP search and skills."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from harness.drive.store import Drive
from .errors import ToolError

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
INNER_WELLS = [f"{r}{c}" for r in "BCDEFG" for c in range(2, 12)]   # perimeter left empty
DOSES_UM = [10 / 2 ** i for i in range(10)]                         # 10 µM, 2-fold, 10 points


def plan_followup_plate(drive: Drive, kind: str, compound: str, treatment_hours: int = 72,
                        seed: int = 0) -> dict:
    """Draft a follow-up plate. Vehicle wells always go on the same plate.

    `kind` is "rerun" (16 vehicle + 16 compound at 1 µM) or "dose_response"
    (10 doses x 3 replicates + 16 vehicle). The draft stays pending until approved."""
    if kind == "rerun":
        wells = [("vehicle", 0.0)] * 16 + [(compound, 1.0)] * 16
    elif kind == "dose_response":
        wells = [("vehicle", 0.0)] * 16 + [(compound, round(d, 4)) for d in DOSES_UM for _ in range(3)]
    else:
        raise ToolError(f"kind must be 'rerun' or 'dose_response', not {kind!r}")
    placement = np.random.default_rng(seed).permutation(INNER_WELLS)[:len(wells)]
    layout = pd.DataFrame([{"well": w, "condition": c, "conc_uM": conc, "dmso_pct": 0.1,
                            "treatment_hours": treatment_hours}
                           for w, (c, conc) in zip(placement, wells)])

    n = len(list(drive.path("findings/plans").glob("*.csv"))) + 1 \
        if drive.path("findings/plans").exists() else 1
    rel = f"findings/plans/plan-{n:03d}-{kind}.csv"
    drive.write_artifact(rel, lambda p: layout.to_csv(p, index=False),
                         tool="plan_followup_plate", inputs=["derived/stats.json"])
    design = (f"16 vehicle wells and 16 {compound} wells at 1 µM on one plate, read at "
              f"{treatment_hours} h. Equal arms, and vehicle on the same plate so {compound} can be "
              "normalised to it (SOP-ASSAY-004 needs at least 8 vehicle wells per plate)"
              if kind == "rerun" else
              f"16 vehicle wells and {compound} at 10 doses (10 µM, 2-fold down) in triplicate, read "
              f"at {treatment_hours} h, with vehicle on the same plate for normalisation")
    drive.write_json(rel.replace(".csv", ".json"), {
        "plan": rel, "kind": kind, "compound": compound, "treatment_hours": treatment_hours,
        "status": "pending_approval", "wells": len(layout),
        "vehicle_wells": int((layout.condition == "vehicle").sum()), "design": design,
        "outer_wells": "left empty (SOP-ASSAY-004 rule 4)"})
    return {"plan": rel, "kind": kind, "wells": len(layout),
            "vehicle_wells_on_plate": int((layout.condition == "vehicle").sum()),
            "status": "pending_approval"}


def _sop_lines(drive: Drive):
    for f in drive.files():
        if f["path"].startswith("sops/") and f["path"].endswith(".md"):
            for line in drive.path(f["path"]).read_text().splitlines():
                if re.match(r"\s*\d+\.", line):
                    yield f["path"], line.strip()


def search_sops(drive: Drive, query: str, top_k: int = 3) -> dict:
    """Keyword search over the lab's SOP rules. Returns the best-matching numbered rules."""
    terms = {t for t in re.findall(r"[a-z0-9-]+", query.lower()) if len(t) > 2}
    if not terms:
        raise ToolError("query needs at least one word longer than 2 letters")
    scored = []
    for src, line in _sop_lines(drive):
        words = set(re.findall(r"[a-z0-9-]+", line.lower()))
        score = len(terms & words)
        if score:
            scored.append({"source": src, "rule": line, "score": score})
    scored.sort(key=lambda r: -r["score"])
    return {"query": query, "matches": scored[:top_k]}


def load_skill(name: str) -> dict:
    """Load a skill: a markdown procedure the Planner can follow."""
    available = sorted(p.stem for p in SKILLS_DIR.glob("*.md"))
    path = SKILLS_DIR / f"{name}.md"
    if name not in available:
        raise ToolError(f"no skill {name!r}. Available: {available}")
    return {"skill": name, "steps": path.read_text()}
