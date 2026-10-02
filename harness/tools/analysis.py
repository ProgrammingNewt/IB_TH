"""Analysis tools: deterministic checks and statistics over the parsed data."""
from __future__ import annotations

import json

import pandas as pd
from scipy import stats

from harness.drive.store import CONTRADICTIONS, Drive
from .errors import ToolError
from .files import METADATA, PROTOCOL, READINGS

VEHICLE = "vehicle"
MIN_VEHICLE_WELLS = 8          # SOP-ASSAY-004, rule 1
HOURS_TOLERANCE = 2            # SOP-ASSAY-004, rule 3
ANALYSIS_SET = "derived/analysis_set.csv"
STATS = "derived/stats.json"


def _need(drive: Drive, rel: str, made_by: str):
    if not drive.path(rel).exists():
        raise ToolError(f"{rel} doesn't exist yet. Run {made_by} first")


def check_consistency(drive: Drive) -> dict:
    """Compare every plate's run metadata and wells with what the protocol requires.

    One record per plate with problems, addressed to both owners. The owner of the data
    file decides, because their data is what an answer would change."""
    _need(drive, PROTOCOL, "extract_protocol_fields")
    _need(drive, METADATA, "parse_xlsx")
    protocol = drive.read_json(PROTOCOL)
    readings = pd.read_csv(drive.path(READINGS))
    existing = {c["id"]: c for c in drive.contradictions()}

    found = []
    for m in drive.read_json(METADATA):
        issues = []
        want = protocol.get("treatment_hours")
        if want is not None and abs(float(m["treatment_hours"]) - want) > HOURS_TOLERANCE:
            issues.append({"field": "treatment_hours", "protocol_value": want,
                           "data_value": m["treatment_hours"]})
        if protocol.get("vehicle_on_every_plate"):
            on_plate = readings[(readings.source_file == m["source_file"])
                                & (readings.plate_id == m["plate_id"])]
            n_vehicle = int((on_plate.condition == VEHICLE).sum())
            if n_vehicle < MIN_VEHICLE_WELLS:
                issues.append({"field": "vehicle_wells",
                               "protocol_value": f">= {MIN_VEHICLE_WELLS}",
                               "data_value": n_vehicle})
        if not issues:
            continue
        cid = f"c-{m['source_file'].split('/')[-1].split('.')[0]}-{m['plate_id']}"
        if cid in existing:                     # keep its status and answer
            found.append(existing[cid])
            continue
        plate = m["plate_id"]
        found.append({
            "id": cid, "status": "open",
            "file": m["source_file"], "plate_id": plate, "batch": int(m["batch"]),
            "issues": issues, "notes": m.get("notes", ""),
            "owners": sorted({protocol["owner"], drive.owner_of(m["source_file"])}),
            "decides": drive.owner_of(m["source_file"]),
            "choices": {"exclude": f"Exclude plate {plate} from the analysis",
                        "keep": f"Keep plate {plate}, the deviation doesn't matter",
                        "other": "Something else (explain)"},
        })

    # Contradictions on files that are no longer flagged stay on record.
    kept = [c for cid, c in existing.items() if cid not in {f["id"] for f in found}]
    drive.write_json(CONTRADICTIONS, kept + found)
    return {"contradictions": found,
            "open": [c["id"] for c in found if c["status"] == "open"],
            "wrote": [CONTRADICTIONS]}


def filter_dataset(drive: Drive) -> dict:
    """Apply the team's confirmed facts to the readings.

    Exclusions come only from facts a file's owner confirmed, and each one applies to the
    exact file and plate it names."""
    _need(drive, READINGS, "parse_xlsx")
    readings = pd.read_csv(drive.path(READINGS))
    used, excluded = [], []
    for f in drive.facts():
        plate = f.get("action", {}).get("exclude_plate")
        if not plate:
            continue
        hit = (readings.source_file == f["scope"]["file"]) & (readings.plate_id == plate)
        if hit.any():
            readings = readings[~hit]
            used.append(f["id"])
            excluded.append({"file": f["scope"]["file"], "plate_id": plate,
                             "fact": f["id"], "confirmed_by": f["confirmed_by"]})
    drive.write_artifact(ANALYSIS_SET, lambda p: readings.to_csv(p, index=False),
                         tool="filter_dataset", inputs=[READINGS], facts_used=used)
    kept = (readings[["source_file", "plate_id"]].drop_duplicates()
            .to_dict(orient="records"))
    return {"kept": kept, "excluded": excluded, "rows": len(readings), "wrote": [ANALYSIS_SET]}


def run_stats(drive: Drive, compound: str, min_drop: float | None = None,
              max_p: float | None = None) -> dict:
    """Normalise each plate to its own vehicle wells, then compare compound with vehicle.

    A plate with no vehicle wells can't be normalised, so it's left out and reported."""
    _need(drive, ANALYSIS_SET, "filter_dataset")
    df = pd.read_csv(drive.path(ANALYSIS_SET))
    if compound not in set(df.condition):
        raise ToolError(f"no {compound} wells in the analysis set")
    df["plate_key"] = df.source_file + ":" + df.plate_id
    veh = df[df.condition == VEHICLE].groupby("plate_key").signal.mean()
    refused = sorted(set(df.plate_key) - set(veh.index))
    use = df[df.plate_key.isin(veh.index) & df.condition.isin([VEHICLE, compound])].copy()
    if use.empty:
        raise ToolError("no plate has vehicle wells, so nothing can be normalised")
    use["norm"] = use.signal / use.plate_key.map(veh) * 100
    t = use.loc[use.condition == compound, "norm"]
    v = use.loc[use.condition == VEHICLE, "norm"]
    drop = float(1 - t.mean() / v.mean())
    p = float(stats.ttest_ind(t, v, equal_var=False).pvalue)

    result = {
        "compound": compound, "method": "per-plate vehicle normalisation, Welch t-test",
        "plates_used": sorted(veh.index), "plates_refused_no_vehicle": refused,
        "n_compound": int(len(t)), "n_vehicle": int(len(v)),
        "mean_compound_pct_of_vehicle": round(float(t.mean()), 2),
        "drop": round(drop, 4), "p": float(f"{p:.3g}"),
    }
    if min_drop is not None and max_p is not None:
        result["threshold"] = {"min_drop": min_drop, "max_p": max_p}
        result["meets_threshold"] = bool(drop >= min_drop and p < max_p)
    drive.write_artifact(STATS, lambda path: path.write_text(json.dumps(result, indent=2)),
                         tool="run_stats", inputs=[ANALYSIS_SET])
    return result | {"wrote": [STATS]}
