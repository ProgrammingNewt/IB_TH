"""Turn a Scenario into the files each scientist would have left on the drive."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import nbformat
import pandas as pd
from nbclient import NotebookClient
from reportlab import rl_config
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from .scenarios import COMPOUND, Scenario

rl_config.invariant = 1          # no creation timestamp or random id in the PDF

PEOPLE = {
    "A": {"name": "Ana Ruiz", "role": "biologist"},
    "B": {"name": "Ben Okafor", "role": "screening scientist"},
    "C": {"name": "Chen Li", "role": "data scientist"},
}
FIXED_TIME = dt.datetime(2026, 9, 18, 9, 0, 0)

PROTOCOL = [
    ("Protocol PRT-017: ABC-12 and senescence in IMR-90 fibroblasts", "title"),
    ("Author: Ana Ruiz (biology). Version 1.2, 2 September 2026.", "body"),
    ("1. Question", "h"),
    ("Does ABC-12 reduce senescence-associated beta-galactosidase (SA-beta-gal) signal in "
     "senescent IMR-90 fibroblasts compared with vehicle?", "body"),
    ("2. Cells", "h"),
    ("IMR-90 fibroblasts at passage 8 to 10. Senescence induced with 10 Gy ionising "
     "radiation, 7 days before treatment.", "body"),
    ("3. Treatment", "h"),
    ("ABC-12 at 1 µM from a 1 mM DMSO stock (0.1% DMSO final). Vehicle: 0.1% DMSO. "
     "Treatment duration: 72 h.", "body"),
    ("4. Plate layout", "h"),
    ("96-well plates with the outer wells left empty. Every plate carries its own vehicle "
     "wells (SOP-ASSAY-004).", "body"),
    ("5. Readout", "h"),
    ("SA-beta-gal fluorescence (C12FDG), read 72 h after treatment, background-subtracted, "
     "one value per well in the 'readings' sheet.", "body"),
    ("6. Analysis", "h"),
    ("Compare ABC-12 wells with vehicle wells. Hit criteria: to be agreed with the team "
     "before analysis.", "body"),
]

SOP = """# SOP-ASSAY-004: Plate controls for cell-based assays

Owner: lab operations. Effective 10 March 2026.

1. Every assay plate carries at least 8 vehicle wells, at the same DMSO percentage as the treated wells.
2. Signals are normalised to the vehicle wells on the same plate. A plate without vehicle wells can't be normalised and is not used for comparisons.
3. Senescence readouts (SA-beta-gal) are read 72 h (plus or minus 2 h) after treatment. A run outside this window is flagged before analysis.
4. Outer wells are left empty to avoid edge effects.
"""

NOTEBOOK_LOAD = """import pandas as pd
from scipy import stats

df = pd.read_excel("readings.xlsx", sheet_name="readings")
df.groupby(["plate_id", "condition"]).signal.describe()"""

NOTEBOOK_POOLED = """vehicle = df[df.condition == "vehicle"].signal
treated = df[df.condition == "ABC-12"].signal

drop = 1 - treated.mean() / vehicle.mean()
p = stats.ttest_ind(treated, vehicle, equal_var=False).pvalue
print(f"n vehicle = {len(vehicle)}, n ABC-12 = {len(treated)}")
print(f"drop = {drop:.1%}, p = {p:.2g}")"""

NOTEBOOK_PER_PLATE = """veh_means = df[df.condition == "vehicle"].groupby("plate_id").signal.mean()
df["norm"] = df.signal / df.plate_id.map(veh_means) * 100

vehicle = df[df.condition == "vehicle"].norm
treated = df[df.condition == "ABC-12"].norm

drop = 1 - treated.mean() / vehicle.mean()
p = stats.ttest_ind(treated, vehicle, equal_var=False).pvalue
print(f"n vehicle = {len(vehicle)}, n ABC-12 = {len(treated)}")
print(f"drop = {drop:.1%}, p = {p:.2g}")"""


def write_protocol_pdf(path: Path) -> None:
    styles = getSampleStyleSheet()
    style = {"title": styles["Title"], "h": styles["Heading3"], "body": styles["BodyText"]}
    story = []
    for text, kind in PROTOCOL:
        story.append(Paragraph(text, style[kind]))
        if kind == "body":
            story.append(Spacer(1, 6))
    doc = SimpleDocTemplate(str(path), pagesize=letter, title="PRT-017", author="Ana Ruiz")
    doc.build(story)


def write_readings_xlsx(path: Path, readings: pd.DataFrame, metadata: pd.DataFrame) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        readings.to_excel(xw, sheet_name="readings", index=False)
        metadata.to_excel(xw, sheet_name="run_metadata", index=False)
        xw.book.properties.creator = "Ben Okafor"
        xw.book.properties.created = FIXED_TIME
        xw.book.properties.modified = FIXED_TIME


def write_notebook(path: Path, style: str, claim: dict) -> None:
    """Write C's notebook and execute it, so it carries real saved outputs."""
    analysis = NOTEBOOK_POOLED if style == "pooled" else NOTEBOOK_PER_PLATE
    verdict = "**Hit**, recommend a dose-response next." if claim["verdict"] == "hit" \
        else "Not a hit."
    nb = nbformat.v4.new_notebook()
    nb.cells = [
        nbformat.v4.new_markdown_cell(f"# {COMPOUND} vs vehicle: SA-beta-gal\nChen Li, data science."),
        nbformat.v4.new_code_cell(NOTEBOOK_LOAD),
        nbformat.v4.new_code_cell(analysis),
        nbformat.v4.new_markdown_cell(
            f"## Conclusion\n{COMPOUND} reduces SA-beta-gal signal by {claim['drop']:.0%} vs "
            f"vehicle (p = {claim['p']:.1g}). {verdict}"),
    ]
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3",
                                 "language": "python"}
    NotebookClient(nb, timeout=120, kernel_name="python3", record_timing=False,
                   resources={"metadata": {"path": str(path.parent)}}).execute()
    nbformat.write(nb, path)


def write_manifest(path: Path, drive: str, files: list[dict]) -> None:
    path.write_text(json.dumps({"drive": drive, "people": PEOPLE, "files": files}, indent=2))


def write_scenario(s: Scenario, out: Path) -> Path:
    """Write the drive for one scenario. Returns the drive folder."""
    drive = out / s.name
    (drive / "raw").mkdir(parents=True)
    (drive / "sops").mkdir()

    write_protocol_pdf(drive / "raw" / "protocol.pdf")
    write_readings_xlsx(drive / "raw" / "readings.xlsx", s.readings, s.metadata)
    write_notebook(drive / "raw" / "analysis.ipynb", s.notebook_style, s.notebook_claim)
    (drive / "sops" / "SOP-ASSAY-004.md").write_text(SOP)
    write_manifest(drive / "manifest.json", s.name, [
        {"path": "raw/protocol.pdf", "owner": "A", "modified": "2026-09-02",
         "description": "Experiment protocol: the question, treatment and readout"},
        {"path": "raw/readings.xlsx", "owner": "B", "modified": "2026-09-16",
         "description": "Plate readings and run metadata for every batch"},
        {"path": "raw/analysis.ipynb", "owner": "C", "modified": "2026-09-18",
         "description": "Analysis of the readings and the team's current conclusion"},
        {"path": "sops/SOP-ASSAY-004.md", "owner": "lab", "modified": "2026-03-10",
         "description": "Lab SOP for plate controls and readout timing"},
    ])

    # Files that arrive later (run 2) and the answer key live beside the drive, not in it,
    # so the harness can't read them until they're uploaded or scored.
    if s.rerun_readings is not None:
        incoming = out / f"{s.name}.incoming"
        incoming.mkdir()
        write_readings_xlsx(incoming / "readings_rerun.xlsx", s.rerun_readings, s.rerun_metadata)
    (out / f"{s.name}.expected.json").write_text(json.dumps(s.expected, indent=2))
    return drive
