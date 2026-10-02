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
