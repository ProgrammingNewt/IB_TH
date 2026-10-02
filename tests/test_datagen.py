import json

import nbformat
import pandas as pd
import pytest
from pypdf import PdfReader

from datagen.__main__ import main
from datagen.scenarios import COMPOUND, VEHICLE, clean, planted_batch_issue


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    out = tmp_path_factory.mktemp("drives")
    assert main(["--out", str(out)]) == 0
    return out


def test_same_seed_gives_same_readings():
    pd.testing.assert_frame_equal(planted_batch_issue().readings, planted_batch_issue().readings)
    pd.testing.assert_frame_equal(clean().readings, clean().readings)


def test_planted_story_holds():
    s = planted_batch_issue()
    assert s.expected["notebook_claim"]["verdict"] == "hit"
    assert s.expected["run1"]["verdict"] == "not a hit"
    assert s.expected["run2"]["verdict"] == "not a hit"
    # batch 2 is treated-only: the confound the scenario depends on
    p2 = s.readings[s.readings.plate_id == "P2"]
    assert set(p2.condition) == {COMPOUND}
    assert s.metadata.set_index("plate_id").treatment_hours.to_dict() == {"P1": 72, "P2": 48}


def test_clean_story_holds():
    s = clean()
    assert s.expected["contradictions"] == []
    assert s.expected["run1"]["verdict"] == "hit"
    for plate in ("P1", "P2"):
        assert VEHICLE in set(s.readings[s.readings.plate_id == plate].condition)


@pytest.mark.parametrize("name", ["planted_batch_issue", "clean"])
def test_drive_files_and_owners(out, name):
    drive = out / name
    manifest = json.loads((drive / "manifest.json").read_text())
    owners = {f["path"]: f["owner"] for f in manifest["files"]}
    assert owners == {"raw/protocol.pdf": "A", "raw/readings.xlsx": "B",
                      "raw/analysis.ipynb": "C", "sops/SOP-ASSAY-004.md": "lab"}
    for path in owners:
        assert (drive / path).is_file()
    assert "72 h" in PdfReader(drive / "raw/protocol.pdf").pages[0].extract_text()
    sheets = pd.read_excel(drive / "raw/readings.xlsx", sheet_name=None)
    assert set(sheets) == {"readings", "run_metadata"}


@pytest.mark.parametrize("name", ["planted_batch_issue", "clean"])
def test_notebook_output_matches_its_conclusion(out, name):
    nb = nbformat.read(out / name / "raw/analysis.ipynb", as_version=4)
    printed = nb.cells[2].outputs[0]["text"]
    claim = json.loads((out / f"{name}.expected.json").read_text())["notebook_claim"]
    assert f"drop = {claim['drop']:.1%}" in printed
    assert f"{claim['drop']:.0%}" in nb.cells[3].source


def test_answer_key_and_later_upload_sit_outside_the_drive(out):
    assert (out / "planted_batch_issue.expected.json").is_file()
    assert (out / "planted_batch_issue.incoming/readings_rerun.xlsx").is_file()
    drive_files = {p.name for p in (out / "planted_batch_issue").rglob("*")}
    assert "readings_rerun.xlsx" not in drive_files
    assert not any(n.endswith(".expected.json") for n in drive_files)
