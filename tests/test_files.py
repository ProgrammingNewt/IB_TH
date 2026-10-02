"""Removing team files, and reading files of any type."""
import json
import zipfile

import pandas as pd
import pytest

from harness.drive.store import DriveError
from harness.state import RunState
from harness.tools.registry import call


def ingest(drive, state):
    for tool, path in [("extract_protocol_fields", "raw/protocol.pdf"),
                       ("parse_xlsx", "raw/readings.xlsx"),
                       ("read_notebook", "raw/analysis.ipynb")]:
        assert call(drive, state, tool, {"path": path})["ok"]
    call(drive, state, "check_consistency")


def test_only_the_owner_can_remove_a_file(make_drive):
    drive, _, _ = make_drive("planted_batch_issue")
    with pytest.raises(DriveError, match="owned by B"):
        drive.remove("raw/readings.xlsx", by="A")
    with pytest.raises(DriveError, match="owned by lab"):
        drive.remove("sops/SOP-ASSAY-004.md", by="A")
    with pytest.raises(DriveError, match="not in the drive"):
        drive.remove("raw/nope.xlsx", by="A")


def test_removing_a_file_moves_it_to_trash_and_forgets_its_data(make_drive):
    drive, _, _ = make_drive("planted_batch_issue")
    state = RunState()
    ingest(drive, state)
    assert drive.open_contradictions()

    out = drive.remove("raw/readings.xlsx", by="B")
    assert drive.owner_of("raw/readings.xlsx") is None
    assert not drive.path("raw/readings.xlsx").exists()
    assert drive.path(out["trash"]).exists()                       # recoverable
    assert drive.manifest["removed"][0]["by"] == "B"
    assert pd.read_csv(drive.path("derived/clean_readings.csv")).empty
    assert drive.read_json("facts/run_metadata.json") == []
    assert drive.contradictions()[0]["status"] == "withdrawn"
    assert not drive.open_contradictions()

    drive.remove("raw/protocol.pdf", by="A")
    assert not drive.path("facts/protocol.json").exists()


def test_upload_replaces_only_for_the_owner(make_drive, tmp_path):
    drive, _, _ = make_drive("planted_batch_issue")
    new = tmp_path / "readings.xlsx"
    new.write_bytes(drive.path("raw/readings.xlsx").read_bytes())
    with pytest.raises(DriveError, match="already exists"):
        drive.upload(new, "B", "v2")
    with pytest.raises(DriveError, match="owned by B"):
        drive.upload(new, "A", "v2", replace=True)
    assert drive.upload(new, "B", "v2", replace=True) == "raw/readings.xlsx"
    assert drive.owner_of("raw/readings.xlsx") == "B"
    assert len(list(drive.path("trash").iterdir())) == 1


def _docx(path, paragraphs):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')


def test_any_file_type_can_be_uploaded_and_read(make_drive, tmp_path):
    drive, _, _ = make_drive("clean")
    state = RunState()
    (tmp_path / "doses.csv").write_text("compound,dose_uM,viability\nABC-12,1,0.95\nABC-12,10,0.71\n")
    (tmp_path / "meeting.txt").write_text("Ana: let's check the 72 h timing before calling a hit.")
    _docx(tmp_path / "summary.docx", ["Weekly summary", "ABC-12 looked promising on plate P1."])
    (tmp_path / "plate.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    for name in ("doses.csv", "meeting.txt", "summary.docx", "plate.png"):
        drive.upload(tmp_path / name, "C", name)

    t = call(drive, state, "read_table", {"path": "raw/doses.csv"})
    assert t["tables"][0]["columns"] == ["compound", "dose_uM", "viability"]
    assert t["tables"][0]["rows"] == 2
    assert "72 h timing" in call(drive, state, "read_document", {"path": "raw/meeting.txt"})["text"]
    assert "looked promising" in call(drive, state, "read_document", {"path": "raw/summary.docx"})["text"]
    assert "72 h" in call(drive, state, "read_document", {"path": "raw/protocol.pdf"})["text"]
    img = call(drive, state, "read_document", {"path": "raw/plate.png"})
    assert not img["ok"] and "can't read .png" in img["error"] and "don't guess" in img["error"]
    sheets = call(drive, state, "read_table", {"path": "raw/readings.xlsx"})["tables"]
    assert [s["table"] for s in sheets] == ["readings", "run_metadata"]
    assert "read_notebook" in call(drive, state, "read_document", {"path": "raw/analysis.ipynb"})["error"]


def test_harness_remove_marks_findings_stale_and_reevaluate_clears_it(make_drive, monkeypatch):
    from harness.gateway.fakes import ScriptedClient
    from harness.gateway.gateway import Gateway, load_config
    from harness.harness import Harness
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    drive, _, _ = make_drive("clean")
    h = Harness(drive, Gateway(load_config(), client_factory=lambda p: ScriptedClient([])))
    h._save("goals", {"id": "g-001", "requester": "A", "question": "Is ABC-12 a hit?", "slots": {},
                      "status": "answered", "runs": [], "history": []})
    h.remove("raw/analysis.ipynb", by="C")
    stale = h._load("goals", "g-001")["stale"]
    assert stale[0]["change"] == "raw/analysis.ipynb removed by Chen Li"
    assert h.reevaluate("test") == []                     # no runs yet, so nothing to re-run


def test_findings_list_files_the_analysis_never_read(make_drive, monkeypatch, tmp_path):
    """Whatever the Planner writes, findings name every team file it didn't read."""
    from harness.gateway.fakes import ScriptedClient
    from harness.gateway.gateway import Gateway, load_config
    from harness.harness import Harness
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    drive, _, _ = make_drive("clean")
    (tmp_path / "plate_photo.png").write_bytes(b"\x89PNG fake")
    drive.upload(tmp_path / "plate_photo.png", "B", "photo")
    h = Harness(drive, Gateway(load_config(), client_factory=lambda p: ScriptedClient([])))
    goal = {"id": "g-001", "requester": "A", "question": "Is ABC-12 a hit?",
            "slots": {"compound": "ABC-12"}, "status": "open", "runs": [], "history": []}
    run = {"id": "r-001"}
    h._after_tool(run, goal, "parse_xlsx", {"path": "raw/readings.xlsx"}, {"ok": True})
    h._after_tool(run, goal, "read_document", {"path": "raw/plate_photo.png"},
                  {"ok": False, "error": "the harness can't read .png files"})
    goal.update(last_draft={"verdict": "hit", "summary": "s", "numbers": [], "next_step": "n"},
                last_run="r-001")
    md = h._render(goal)
    unused = md.split("## Files not used")[1]
    assert "raw/plate_photo.png` (Ben Okafor): the harness can't read .png files" in unused
    assert "raw/protocol.pdf` (Ana Ruiz): not read in this analysis" in unused
    assert "readings.xlsx" not in unused
