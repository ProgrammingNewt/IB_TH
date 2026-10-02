"""The Streamlit pages render for every scientist, and background jobs report back.

Rendering makes no model calls, so this runs offline.
"""
import shutil
import sys
import time
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "ui"


@pytest.fixture
def workspace(generated, tmp_path, monkeypatch):
    for name in ("planted_batch_issue", "clean"):
        shutil.copytree(generated / name, tmp_path / name)
    shutil.copytree(generated / "planted_batch_issue.incoming", tmp_path / "planted_batch_issue.incoming")
    monkeypatch.setenv("HARNESS_WORKSPACE", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("page", ["lab.py", "playground.py"])
@pytest.mark.parametrize("who", ["A", "B", "C"])
def test_pages_render(workspace, page, who):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(UI / page), default_timeout=120)
    at.query_params["drive"] = "planted_batch_issue"
    at.query_params["as"] = who
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    if page == "lab.py":
        assert [t.label for t in at.tabs][:5] == [
            "💬 Ask", "❓ My questions (0)", "📁 Drive", "📊 Findings", "🔍 Trace"]


def test_job_runner_runs_one_job_at_a_time():
    sys.path.insert(0, str(UI))
    from common import JobRunner
    r = JobRunner()
    assert r.submit("d", "slow", "A", lambda: time.sleep(0.3) or "done")
    assert not r.submit("d", "second", "B", lambda: None)        # busy
    while r.busy():
        time.sleep(0.05)
    assert r.history[0].result == "done" and r.last_finished_id() == 1
    assert r.submit("d", "boom", "A", lambda: 1 / 0)
    while r.busy():
        time.sleep(0.05)
    assert r.history[0].error.startswith("ZeroDivisionError")


def test_start_fresh(workspace, tmp_path):
    sys.path.insert(0, str(UI))
    import importlib
    import common
    importlib.reload(common)                        # pick up HARNESS_WORKSPACE
    root = workspace / "planted_batch_issue"
    for folder in ("goals", "runs", "messages", "findings"):
        (root / folder).mkdir()
        (root / folder / "x.json").write_text("{}")
    upload = tmp_path / "mine.txt"
    upload.write_text("my notes")
    from harness.drive.store import Drive
    Drive(root).upload(upload, "C", "notes")

    assert set(common.clear_harness_work("planted_batch_issue")) == {"goals", "runs", "messages", "findings"}
    assert not (root / "goals").exists() and (root / "raw/mine.txt").exists()     # uploads kept

    assert common.reset_drive("planted_batch_issue") == ""                          # back to generated
    assert not (root / "raw/mine.txt").exists() and (root / "raw/readings.xlsx").exists()
    assert common.generated_from("planted_batch_issue") == ("planted_batch_issue", 20261002)


def test_clear_everything(workspace, tmp_path):
    sys.path.insert(0, str(UI))
    import importlib
    import common
    importlib.reload(common)
    from harness.drive.store import Drive
    for name in ("planted_batch_issue", "clean"):
        root = workspace / name
        for folder in common.HARNESS_FOLDERS:
            (root / folder).mkdir(exist_ok=True)
            (root / folder / "x.json").write_text("{}")
        note = tmp_path / f"{name}-notes.txt"
        note.write_text("mine")
        Drive(root).upload(note, "C", "notes")
    Drive(workspace / "clean").remove("raw/analysis.ipynb", by="C")

    assert len(common.clear_everything()) == 2
    for name in ("planted_batch_issue", "clean"):
        root = workspace / name
        assert sorted(p.name for p in root.iterdir()) == ["manifest.json", "raw", "sops"]
        assert sorted(p.name for p in (root / "raw").iterdir()) == [
            "analysis.ipynb", "protocol.pdf", "readings.xlsx"]          # uploads gone, removals undone
