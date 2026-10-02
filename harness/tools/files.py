"""File tools: the only code that opens a scientist's raw file."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

import nbformat
import pandas as pd
from nbclient import NotebookClient
from pypdf import PdfReader

from harness.drive.store import Drive
from .errors import ToolError

READINGS = "derived/clean_readings.csv"
METADATA = "facts/run_metadata.json"
PROTOCOL = "facts/protocol.json"
NOTEBOOK_CLAIMS = "facts/notebook_claims.json"
READING_COLUMNS = {"plate_id", "batch", "well", "condition", "signal"}
METADATA_COLUMNS = {"plate_id", "batch", "treatment_hours"}


def _require_raw(drive: Drive, path: str, suffix: str) -> Path:
    if not path.endswith(suffix):
        raise ToolError(f"{path} is not a {suffix} file")
    if drive.owner_of(path) is None:
        raise ToolError(f"{path} is not in the drive manifest")
    return drive.path(path)


def parse_xlsx(drive: Drive, path: str) -> dict:
    """Read a readings workbook into tidy rows plus per-plate run metadata.

    Rows from several workbooks accumulate in one table, each tagged with its source file,
    so a re-run uploaded later joins the earlier data instead of replacing it."""
    sheets = pd.read_excel(_require_raw(drive, path, ".xlsx"), sheet_name=None)
    if "readings" not in sheets or "run_metadata" not in sheets:
        raise ToolError(f"{path} needs 'readings' and 'run_metadata' sheets, has {list(sheets)}")
    readings, meta = sheets["readings"], sheets["run_metadata"]
    for name, df, cols in (("readings", readings, READING_COLUMNS),
                           ("run_metadata", meta, METADATA_COLUMNS)):
        missing = cols - set(df.columns)
        if missing:
            raise ToolError(f"{path} sheet {name} is missing columns {sorted(missing)}")
    readings = readings.assign(source_file=path)
    meta = meta.assign(source_file=path).fillna("")

    existing = pd.read_csv(drive.path(READINGS)) if drive.path(READINGS).exists() else None
    if existing is not None:
        readings = pd.concat([existing[existing.source_file != path], readings], ignore_index=True)
    drive.write_artifact(READINGS, lambda p: readings.to_csv(p, index=False),
                         tool="parse_xlsx", inputs=sorted(readings.source_file.unique()))

    all_meta = [m for m in drive.read_json(METADATA, []) if m["source_file"] != path]
    all_meta += meta.to_dict(orient="records")
    drive.write_artifact(METADATA, lambda p: p.write_text(json.dumps(all_meta, indent=2,
        default=str)), tool="parse_xlsx",
        inputs=sorted({m["source_file"] for m in all_meta}))

    this = readings[readings.source_file == path]
    plates = []
    for m in meta.to_dict(orient="records"):
        counts = this[this.plate_id == m["plate_id"]].condition.value_counts().to_dict()
        plates.append({"plate_id": m["plate_id"], "batch": int(m["batch"]),
                       "treatment_hours": m["treatment_hours"], "wells": counts,
                       "notes": m.get("notes", "")})
    return {"file": path, "plates": plates, "wrote": [READINGS, METADATA]}


# ---- protocol -----------------------------------------------------------------

def regex_protocol_fields(text: str) -> dict:
    """Offline extractor for the protocol's fixed fields.

    The harness normally uses an LLM here, because a real protocol's wording varies. This
    version keeps the tool working (and testable) with no model at all."""
    flat = " ".join(text.split())

    def grab(pattern: str):
        m = re.search(pattern, flat, re.IGNORECASE)
        return m.group(1).strip() if m else None

    hours = grab(r"Treatment duration:\s*(\d+)\s*h")
    hit = grab(r"Hit criteria:\s*([^.]+)")
    return {
        "compound": grab(r"Does (\S+) reduce"),
        "readout": grab(r"\((SA-beta-gal)\)"),
        "comparator": grab(r"compared with (\w+)"),
        "control": grab(r"Vehicle:\s*([^.]+)"),
        "treatment_hours": int(hours) if hours else None,
        "vehicle_on_every_plate": "every plate carries its own vehicle" in flat.lower(),
        "hit_criteria": None if not hit or "to be agreed" in hit.lower() else hit,
    }


def extract_protocol_fields(drive: Drive, path: str,
                            extractor: Callable[[str], dict] | None = None) -> dict:
    """Read the protocol PDF's text in code, then fill fixed fields from it."""
    reader = PdfReader(_require_raw(drive, path, ".pdf"))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    fields = (extractor or regex_protocol_fields)(text)
    record = {"source_file": path, "owner": drive.owner_of(path), **fields}
    drive.write_artifact(PROTOCOL, lambda p: p.write_text(json.dumps(record, indent=2)),
                         tool="extract_protocol_fields", inputs=[path])
    missing = [k for k, v in fields.items() if v in (None, "")]
    return {"file": path, "fields": fields, "not_stated": missing, "wrote": [PROTOCOL]}


# ---- notebooks ------------------------------------------------------------------

def _cell_text(cell) -> str:
    return "".join(o.get("text", "") for o in cell.get("outputs", []))


def read_notebook(drive: Drive, path: str) -> dict:
    """Read a notebook's code, saved outputs and written conclusion."""
    nb = nbformat.read(_require_raw(drive, path, ".ipynb"), as_version=4)
    code = [{"source": c.source, "output": _cell_text(c)} for c in nb.cells if c.cell_type == "code"]
    conclusion = next((c.source for c in reversed(nb.cells)
                       if c.cell_type == "markdown" and "conclusion" in c.source.lower()), "")
    drop = re.search(r"by (\d+(?:\.\d+)?)%", conclusion)
    p = re.search(r"p = ([\d.]+(?:e-?\d+)?)", conclusion)
    all_code = "\n".join(c["source"] for c in code)
    claims = {
        "source_file": path,
        "owner": drive.owner_of(path),
        "conclusion": conclusion,
        "claimed_drop": float(drop.group(1)) / 100 if drop else None,
        "claimed_p": float(p.group(1)) if p else None,
        "claimed_verdict": "not a hit" if "not a hit" in conclusion.lower()
                           else "hit" if "hit" in conclusion.lower() else None,
        # Heuristic, for the Planner to weigh rather than to decide on.
        "normalises_per_plate": 'groupby("plate_id")' in all_code and "vehicle" in all_code,
        "code": code,
    }
    drive.write_artifact(NOTEBOOK_CLAIMS, lambda p: p.write_text(
        json.dumps(claims, indent=2)), tool="read_notebook", inputs=[path])
    return {k: v for k, v in claims.items() if k != "code"} | {"code_cells": len(code),
                                                            "wrote": [NOTEBOOK_CLAIMS]}


def run_notebook(drive: Drive, path: str) -> dict:
    """Re-run a notebook on a copy of the raw folder and compare with its saved outputs."""
    src = _require_raw(drive, path, ".ipynb")
    nb = nbformat.read(src, as_version=4)
    saved = [_cell_text(c) for c in nb.cells if c.cell_type == "code"]
    with tempfile.TemporaryDirectory() as tmp:
        for f in src.parent.iterdir():
            if f.is_file():
                shutil.copy2(f, tmp)
        try:
            NotebookClient(nb, timeout=120, kernel_name="python3", record_timing=False,
                           resources={"metadata": {"path": tmp}}).execute()
        except Exception as exc:                                    # noqa: BLE001
            raise ToolError(f"{path} failed to run: {str(exc)[:300]}") from exc
    fresh = [_cell_text(c) for c in nb.cells if c.cell_type == "code"]
    result = {"source_file": path, "reproduced": fresh == saved,
              "outputs": [o for o in fresh if o.strip()]}
    rel = "derived/notebook_repro.json"
    drive.write_artifact(rel, lambda p: p.write_text(json.dumps(result, indent=2)),
                         tool="run_notebook", inputs=[path])
    return result | {"wrote": [rel]}
