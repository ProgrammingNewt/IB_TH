"""Tools and guardrails against the generated drives, with no LLM involved.

The two flow tests walk the same path the agents will take, answering the scientists'
questions by hand, and check every number against the scenario's answer key.
"""
import pytest

from harness.state import RunState
from harness.tools.registry import call, openai_tools

THRESHOLD = {"min_drop": 0.30, "max_p": 0.05}


def ingest(drive, state):
    for tool, path in [("extract_protocol_fields", "raw/protocol.pdf"),
                       ("parse_xlsx", "raw/readings.xlsx"),
                       ("read_notebook", "raw/analysis.ipynb")]:
        r = call(drive, state, tool, {"path": path})
        assert r["ok"], r


def test_planted_run1_and_run2(make_drive):
    drive, expected, incoming = make_drive("planted_batch_issue")
    state = RunState(goal={"compound": "ABC-12"})
    ingest(drive, state)

    # The protocol answers readout and comparison, but leaves the hit criteria open.
    protocol = drive.read_json("facts/protocol.json")
    assert protocol["treatment_hours"] == 72 and protocol["hit_criteria"] is None
    assert protocol["control"] == "0.1% DMSO"
    state.goal |= {"readout": protocol["readout"], "comparator": protocol["comparator"]}
    assert state.missing_slots() == ["threshold"]

    # The notebook claims a hit.
    claims = drive.read_json("facts/notebook_claims.json")
    assert claims["claimed_verdict"] == "hit"
    assert claims["normalises_per_plate"] is False

    # Consistency finds plate P2: 48 h and no vehicle wells, owned by B.
    r = call(drive, state, "check_consistency")
    [c] = r["contradictions"]
    want = expected["contradictions"][0]
    assert c["id"] == want["id"] and c["decides"] == want["decides"]
    assert c["owners"] == want["owners"]
    assert [i["field"] for i in c["issues"]] == want["issues"]

    # Analysis is blocked while the contradiction is open.
    blocked = call(drive, state, "filter_dataset")
    assert not blocked["ok"] and "c-readings-P2" in blocked["blocked"]

    # A isn't the owner of readings.xlsx, so A's answer is only a comment.
    r = drive.resolve_contradiction(c["id"], by="A", choice="exclude")
    assert r["recorded_as"] == "comment" and drive.open_contradictions()
    # B's answer becomes a fact scoped to that file and plate.
    r = drive.resolve_contradiction(c["id"], by="B", choice=want["scripted_answer"])
    assert r["recorded_as"] == "fact" and not drive.open_contradictions()
    assert r["fact"]["scope"] == {"file": "raw/readings.xlsx", "plate_id": "P2", "batch": 2}

    # Threshold still missing, so run_stats stays blocked even with the contradiction resolved.
    assert call(drive, state, "filter_dataset")["ok"]
    r = call(drive, state, "run_stats", {"compound": "ABC-12", **THRESHOLD})
    assert not r["ok"] and "threshold" in r["blocked"]

    state.goal["threshold"] = THRESHOLD
    stats = call(drive, state, "run_stats", {"compound": "ABC-12", **THRESHOLD})
    assert stats["ok"]
    assert stats["plates_used"] == ["raw/readings.xlsx:P1"]
    assert stats["drop"] == pytest.approx(expected["run1"]["drop"], abs=1e-4)
    assert stats["p"] == pytest.approx(expected["run1"]["p"], rel=1e-2)
    assert stats["meets_threshold"] is False

    lineage = drive.read_json("derived/analysis_set.csv.lineage.json")
    assert lineage["facts_used"] == [r_fact["id"] for r_fact in drive.facts()]

    plan = call(drive, state, "plan_followup_plate", {"kind": "rerun", "compound": "ABC-12"})
    assert plan["ok"] and plan["approval"].startswith("pending")
    assert plan["vehicle_wells_on_plate"] == 16

    # Run 2: B uploads the re-run. Nothing new to ask, and the old fact keeps P2 out
    # without touching P3.
    rel = drive.upload(incoming / "readings_rerun.xlsx", owner="B",
                       description="Re-run at 72 h with vehicle on the plate")
    state2 = RunState(goal=dict(state.goal))
    assert call(drive, state2, "parse_xlsx", {"path": rel})["ok"]
    r = call(drive, state2, "check_consistency")
    assert r["open"] == []
    kept = call(drive, state2, "filter_dataset")["kept"]
    assert {(k["source_file"], k["plate_id"]) for k in kept} == {
        ("raw/readings.xlsx", "P1"), ("raw/readings_rerun.xlsx", "P3")}
    stats2 = call(drive, state2, "run_stats", {"compound": "ABC-12", **THRESHOLD})
    assert stats2["drop"] == pytest.approx(expected["run2"]["drop"], abs=1e-4)
    assert stats2["meets_threshold"] is False


def test_clean_flow(make_drive):
    drive, expected, _ = make_drive("clean")
    state = RunState(goal={"compound": "ABC-12", "readout": "SA-beta-gal",
                           "comparator": "vehicle", "threshold": THRESHOLD})
    ingest(drive, state)
    assert call(drive, state, "check_consistency")["contradictions"] == []
    assert call(drive, state, "filter_dataset")["excluded"] == []
    stats = call(drive, state, "run_stats", {"compound": "ABC-12", **THRESHOLD})
    assert stats["drop"] == pytest.approx(expected["run1"]["drop"], abs=1e-4)
    assert stats["meets_threshold"] is True
    claims = drive.read_json("facts/notebook_claims.json")
    assert claims["claimed_verdict"] == "hit" and claims["normalises_per_plate"] is True


def test_run_notebook_reproduces_the_saved_claim(make_drive):
    drive, expected, _ = make_drive("planted_batch_issue")
    r = call(drive, RunState(), "run_notebook", {"path": "raw/analysis.ipynb"})
    assert r["ok"] and r["reproduced"] is True
    assert f"drop = {expected['notebook_claim']['drop']:.1%}" in "".join(r["outputs"])


def test_run_stats_refuses_plates_without_vehicle(make_drive):
    """If P2 were kept, it still couldn't fake a hit: it has no vehicle to normalise to."""
    drive, _, _ = make_drive("planted_batch_issue")
    state = RunState(goal={"compound": "ABC-12", "readout": "x", "comparator": "vehicle",
                           "threshold": THRESHOLD})
    ingest(drive, state)
    call(drive, state, "check_consistency")
    drive.resolve_contradiction("c-readings-P2", by="B", choice="keep")
    call(drive, state, "filter_dataset")
    stats = call(drive, state, "run_stats", {"compound": "ABC-12", **THRESHOLD})
    assert stats["plates_refused_no_vehicle"] == ["raw/readings.xlsx:P2"]
    assert stats["meets_threshold"] is False


def test_guardrails_reject_bad_calls(make_drive):
    drive, _, _ = make_drive("clean")
    state = RunState(budget=2)
    assert "no tool" in call(drive, state, "delete_everything")["blocked"]
    assert "missing required" in call(drive, state, "parse_xlsx", {})["blocked"]
    assert "unknown arguments" in call(drive, state, "search_sops",
                                       {"query": "vehicle", "x": 1})["blocked"]
    assert call(drive, state, "search_sops", {"query": "vehicle wells"})["ok"]
    assert call(drive, state, "search_sops", {"query": "72 h readout"})["ok"]
    assert "budget" in call(drive, state, "search_sops", {"query": "vehicle"})["blocked"]
    assert state.tool_calls == 2


def test_run_stats_must_use_the_requesters_threshold(make_drive):
    drive, _, _ = make_drive("clean")
    state = RunState(goal={"compound": "ABC-12", "readout": "x", "comparator": "vehicle",
                           "threshold": THRESHOLD})
    ingest(drive, state)
    call(drive, state, "filter_dataset")
    r = call(drive, state, "run_stats", {"compound": "ABC-12", "min_drop": 0.1, "max_p": 0.5})
    assert not r["ok"] and "threshold the requester gave" in r["blocked"]


def test_tool_errors_come_back_as_results(make_drive):
    drive, _, _ = make_drive("clean")
    state = RunState()
    r = call(drive, state, "parse_xlsx", {"path": "raw/protocol.pdf"})
    assert r == {"ok": False, "error": "raw/protocol.pdf is not a .xlsx file"}
    r = call(drive, state, "parse_xlsx", {"path": "../../etc/passwd.xlsx"})
    assert not r["ok"]
    assert "doesn't exist yet" in call(drive, state, "check_consistency")["error"]


def test_search_sops_and_skills(make_drive):
    drive, _, _ = make_drive("clean")
    state = RunState()
    r = call(drive, state, "search_sops", {"query": "vehicle wells on the same plate"})
    assert "vehicle wells" in r["matches"][0]["rule"]
    assert "check_consistency" in call(drive, state, "load_skill", {"name": "batch_qc"})["steps"]
    assert "Available" in call(drive, state, "load_skill", {"name": "nope"})["error"]


def test_tool_list_for_the_planner():
    names = [t["function"]["name"] for t in openai_tools()]
    assert len(names) == len(set(names)) == 12
