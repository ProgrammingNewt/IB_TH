"""The numbers behind each toy scenario, and the checks that make its story true.

A scenario is seeded, so the same seed always produces the same readings. After drawing
them, `_require` re-checks the claims the scenario depends on and raises if chance made
one false. The checks use scipy directly rather than the harness's own tools, so a bug
in a tool can't hide behind the data it's tested on.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

DEFAULT_SEED = 20261002
COMPOUND = "ABC-12"
VEHICLE = "vehicle"
N_PER_ARM = 16
# 96-well plate with the perimeter left empty (SOP-ASSAY-004): rows B-G, columns 2-11.
INNER_WELLS = [f"{r}{c}" for r in "BCDEFG" for c in range(2, 12)]

# The threshold Scientist A gives when asked. Scripted here so eval runs repeat exactly.
SCRIPTED_THRESHOLD = {"min_drop": 0.30, "max_p": 0.05}


class ScenarioCheckFailed(Exception):
    """The drawn numbers don't support the scenario's story. Try another seed."""


@dataclass
class Scenario:
    name: str
    readings: pd.DataFrame
    metadata: pd.DataFrame
    notebook_style: str                 # "pooled" (the planted mistake) or "per_plate"
    notebook_claim: dict
    expected: dict
    rerun_readings: pd.DataFrame | None = None
    rerun_metadata: pd.DataFrame | None = None
    checks: list[str] = field(default_factory=list)


def _well_key(well: str) -> tuple[str, int]:
    return well[0], int(well[1:])


def _plate(rng: np.random.Generator, plate_id: str, batch: int,
           arms: dict[str, tuple[float, float]]) -> pd.DataFrame:
    """One plate: N_PER_ARM wells per arm, placed at random on the inner wells."""
    wells = list(rng.permutation(INNER_WELLS))
    rows = []
    for condition, (mean, sd) in arms.items():
        for value in rng.normal(mean, sd, N_PER_ARM):
            rows.append({
                "plate_id": plate_id,
                "batch": batch,
                "well": wells.pop(),
                "condition": condition,
                "conc_uM": 1.0 if condition == COMPOUND else 0.0,
                "dmso_pct": 0.1,
                "signal": round(float(max(value, 0.5)), 2),
            })
    df = pd.DataFrame(rows)
    return df.sort_values("well", key=lambda s: s.map(_well_key)).reset_index(drop=True)


def _drop(treated, vehicle) -> float:
    return float(1 - np.mean(treated) / np.mean(vehicle))


def _p(treated, vehicle) -> float:
    return float(stats.ttest_ind(treated, vehicle, equal_var=False).pvalue)


def _pooled(df: pd.DataFrame) -> tuple[float, float]:
    """What a pooled analysis reports: every treated well against every vehicle well."""
    t = df.loc[df.condition == COMPOUND, "signal"]
    v = df.loc[df.condition == VEHICLE, "signal"]
    return _drop(t, v), _p(t, v)


def _per_plate(df: pd.DataFrame) -> tuple[float, float]:
    """The correct analysis: each plate normalised to its own vehicle wells.

    A plate with no vehicle wells can't be normalised, so it drops out."""
    veh = df[df.condition == VEHICLE].groupby("plate_id").signal.mean()
    usable = df[df.plate_id.isin(veh.index)]
    norm = usable.signal / usable.plate_id.map(veh) * 100
    t = norm[usable.condition == COMPOUND]
    v = norm[usable.condition == VEHICLE]
    return _drop(t, v), _p(t, v)


def _verdict(drop: float, p: float) -> str:
    th = SCRIPTED_THRESHOLD
    return "hit" if drop >= th["min_drop"] and p < th["max_p"] else "not a hit"


def _require(checks: list[str], ok: bool, message: str) -> None:
    if not ok:
        raise ScenarioCheckFailed(message)
    checks.append(message)


def _metadata(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["plate_id", "batch", "run_date", "treatment_hours",
                                       "operator", "notes"])


def planted_batch_issue(seed: int = DEFAULT_SEED) -> Scenario:
    """Batch 2 ran 48 h with treated wells only. Pooling it fakes a hit."""
    rng = np.random.default_rng(seed)
    p1 = _plate(rng, "P1", 1, {VEHICLE: (100, 12), COMPOUND: (96, 12)})
    p2 = _plate(rng, "P2", 2, {COMPOUND: (25, 8)})
    p3 = _plate(rng, "P3", 3, {VEHICLE: (100, 12), COMPOUND: (97, 12)})

    readings = pd.concat([p1, p2], ignore_index=True)
    metadata = _metadata([
        ("P1", 1, "2026-09-08", 72, "B", ""),
        ("P2", 2, "2026-09-15", 48, "B", "top-up run, ABC-12 wells only"),
    ])
    rerun_metadata = _metadata([
        ("P3", 3, "2026-09-29", 72, "B", "re-run at 72 h, vehicle on the same plate"),
    ])

    checks: list[str] = []
    b1_drop, b1_p = _pooled(p1)
    _require(checks, b1_p > 0.05 and b1_drop < 0.30,
             f"batch 1 alone shows no effect (drop {b1_drop:.1%}, p={b1_p:.3f})")
    nb_drop, nb_p = _pooled(readings)
    _require(checks, nb_drop >= 0.30 and nb_p < 0.01,
             f"pooling batches 1+2 fakes a hit (drop {nb_drop:.1%}, p={nb_p:.1e})")
    r2_drop, r2_p = _per_plate(pd.concat([p1, p3], ignore_index=True))
    _require(checks, r2_p > 0.05 and r2_drop < 0.30,
             f"batches 1+3, per-plate, still no effect (drop {r2_drop:.1%}, p={r2_p:.3f})")

    expected = {
        "scenario": "planted_batch_issue",
        "threshold_question_to": "A",
        "scripted_threshold": SCRIPTED_THRESHOLD,
        "contradictions": [{
            "field": "treatment_hours", "protocol_value": 72, "data_value": 48,
            "file": "raw/readings.xlsx", "plate_id": "P2", "batch": 2,
            "owners": ["A", "B"], "decides": "B",
            "scripted_answer": "exclude batch 2",
        }],
        "notebook_claim": {"drop": nb_drop, "p": nb_p, "verdict": _verdict(nb_drop, nb_p)},
        "run1": {"analysed": ["P1"], "drop": b1_drop, "p": b1_p,
                 "verdict": _verdict(b1_drop, b1_p)},
        "followup": "72 h re-run with vehicle and ABC-12 wells on the same plate",
        "run2": {"upload": "readings_rerun.xlsx", "analysed": ["P1", "P3"],
                 "drop": r2_drop, "p": r2_p, "verdict": _verdict(r2_drop, r2_p),
                 "questions_to_owners": []},
    }
    return Scenario("planted_batch_issue", readings, metadata, "pooled",
                    expected["notebook_claim"], expected, p3, rerun_metadata, checks)


def clean(seed: int = DEFAULT_SEED) -> Scenario:
    """Everything follows the protocol and ABC-12 has a real effect."""
    rng = np.random.default_rng(seed + 1)
    p1 = _plate(rng, "P1", 1, {VEHICLE: (100, 12), COMPOUND: (60, 10)})
    p2 = _plate(rng, "P2", 2, {VEHICLE: (100, 12), COMPOUND: (62, 10)})
    readings = pd.concat([p1, p2], ignore_index=True)
    metadata = _metadata([
        ("P1", 1, "2026-09-08", 72, "B", ""),
        ("P2", 2, "2026-09-15", 72, "B", "second replicate plate"),
    ])

    checks: list[str] = []
    drop, p = _per_plate(readings)
    _require(checks, drop >= 0.30 and p < 0.05,
             f"per-plate analysis shows a real drop (drop {drop:.1%}, p={p:.1e})")

    expected = {
        "scenario": "clean",
        "threshold_question_to": "A",
        "scripted_threshold": SCRIPTED_THRESHOLD,
        "contradictions": [],
        "notebook_claim": {"drop": drop, "p": p, "verdict": _verdict(drop, p)},
        "run1": {"analysed": ["P1", "P2"], "drop": drop, "p": p,
                 "verdict": _verdict(drop, p)},
        "followup": "10-point dose-response for ABC-12",
    }
    return Scenario("clean", readings, metadata, "per_plate",
                    expected["notebook_claim"], expected, checks=checks)


SCENARIOS = {"planted_batch_issue": planted_batch_issue, "clean": clean}
