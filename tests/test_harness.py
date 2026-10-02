"""The whole harness on the planted drive, with a scripted model.

The fake model's replies are in call order: Router, Planner turns, the protocol
extractor, Q&A messages, the Reviewer. Everything else (tools, guardrails, pausing,
resuming, facts, findings) is the real code. A live-model version of this lives in evals.
"""
import json

import pytest

from harness.gateway.fakes import ScriptedClient
from harness.gateway.gateway import Gateway, load_config
from harness.harness import Harness


def tools(*calls):
    return {"content": "", "finish_reason": "tool_calls",
            "tool_calls": [{"id": f"call_{name}_{i}", "name": name, "arguments": json.dumps(args)}
                           for i, (name, args) in enumerate(calls)]}


def say(**obj):
    return json.dumps(obj)


PROTOCOL_FIELDS = say(compound="ABC-12", readout="SA-beta-gal", comparator="vehicle",
                      control="0.1% DMSO", treatment_hours=72, vehicle_on_every_plate=True,
                      hit_criteria=None)
THRESHOLD = {"min_drop": 0.3, "max_p": 0.05}


@pytest.fixture
def setup(make_drive, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_MODEL", raising=False)
    drive, expected, incoming = make_drive("planted_batch_issue")
    client = ScriptedClient([])
    gw = Gateway(load_config(), client_factory=lambda p: client, sleep=lambda s: None)
    return Harness(drive, gw), client, expected, incoming


def conclusion(drop, p, plan=None, summary=None, value_drop=None):
    return tools(("submit_conclusion", {
        "verdict": "not a hit",
        "summary": summary or ("No evidence of an effect in this data. The notebook's 37% drop "
                               "came from pooling plate P2, a 48 h run with no vehicle wells."),
        "numbers": [{"label": "Drop vs vehicle", "value": value_drop if value_drop is not None else drop,
                     "source": "derived/stats.json", "key": "drop"},
                    {"label": "p-value", "value": p, "source": "derived/stats.json", "key": "p"}],
        "notes_for_owners": [{"owner": "C", "file": "raw/analysis.ipynb",
                              "note": "The analysis pools plate P2, which has no vehicle wells. "
                                      "Normalise each plate to its own vehicle."}],
        "next_step": "Re-run at 72 h with vehicle and ABC-12 on the same plate.",
        **({"plan": plan} if plan else {})}))


def test_run1_pauses_for_two_owners_then_run2_reuses_their_answers(setup):
    h, client, expected, incoming = setup
    r1 = expected["run1"]
    client.replies += [
        say(intent="new_analysis", slots={"compound": "ABC-12"}),                    # router
        tools(("extract_protocol_fields", {"path": "raw/protocol.pdf"}),
              ("parse_xlsx", {"path": "raw/readings.xlsx"}),
              ("read_notebook", {"path": "raw/analysis.ipynb"})),                     # planner
        PROTOCOL_FIELDS,                                                              # extractor
        tools(("check_consistency", {}),
              ("run_stats", {"compound": "ABC-12", "min_drop": 0.3, "max_p": 0.05})),  # planner tries too early
        tools(("ask_scientists", {"questions": [
            {"about": "contradiction:c-readings-P2", "text": "Plate P2 ran 48 h with no vehicle. Exclude it?"},
            {"about": "slot:threshold", "text": "What counts as a hit?",
             "choices": [{"label": "30% drop, p < 0.05", "value": THRESHOLD},
                         {"label": "50% drop, p < 0.01", "value": {"min_drop": 0.5, "max_p": 0.01}}]},
        ]})),                                                                         # planner
        say(message="Hi Ben, plate P2 ran 48 h with no vehicle. Should we exclude it?"),   # qa ask
        say(message="Ana, Ben was asked whether to exclude plate P2."),                   # qa copy
        say(labels=["At least a 30% drop, p < 0.05", "At least a 50% drop, p < 0.01"]),  # qa labels
        say(message="Hi Ana, what drop should count as a hit?"),                          # qa ask
    ]
    out = h.message("A", "Is ABC-12 a hit?")
    assert out["status"] == "waiting"
    q_plate, q_threshold = out["questions"]

    # The guardrail stopped the early run_stats, and the Planner saw why.
    run = h._load("runs", out["run"])
    blocked = [json.loads(m["content"]) for m in run["messages"] if m["role"] == "tool"
               and "blocked" in json.loads(m["content"])]
    assert "c-readings-P2" in blocked[0]["blocked"]

    # Questions went to the right people; the protocol's slots were never asked.
    qs = {q["id"]: q for q in h.questions()}
    assert qs[q_plate]["to"] == "B" and qs[q_plate]["cc"] == ["A"]
    assert [c["key"] for c in qs[q_plate]["choices"]] == ["exclude", "keep", "other"]
    assert qs[q_threshold]["to"] == "A"
    assert h._load("goals", out["goal"])["slots"] == {
        "compound": "ABC-12", "readout": "SA-beta-gal", "comparator": "vehicle"}
    assert [m["kind"] for m in h.inbox("B")] == ["question"]
    assert [m["kind"] for m in h.inbox("A")] == ["fyi", "question"]
    # Every message a scientist received was written by the Q&A agent.
    assert h.inbox("A")[0]["text"] == "Ana, Ben was asked whether to exclude plate P2."
    assert [c["label"] for c in qs[q_threshold]["choices"]] == [
        "At least a 30% drop, p < 0.05", "At least a 50% drop, p < 0.01",
        "Something else (type it)"]

    # A can't decide about B's file. B can. The run waits for the last open question.
    assert h.answer(q_plate, "A", "exclude")["recorded_as"] == "comment"
    r = h.answer(q_plate, "B", "exclude")
    assert r["recorded_as"] == "fact" and r["still_open"] == [q_threshold]

    stats_run1 = {"drop": round(r1["drop"], 4), "p": float(f"{r1['p']:.3g}")}
    client.replies += [
        tools(("filter_dataset", {}),
              ("run_stats", {"compound": "ABC-12", "min_drop": 0.3, "max_p": 0.05})),
        tools(("plan_followup_plate", {"kind": "rerun", "compound": "ABC-12"})),
        say(message="Hi Ana, here's a re-run plate to approve."),                      # qa (approval)
        conclusion(**stats_run1, plan="findings/plans/plan-001-rerun.csv"),
        say(**{"pass": True, "problems": []}),                                          # reviewer
        say(message="Chen, there's a note on analysis.ipynb about plate P2."),         # qa note
        say(message="Ana, ABC-12 isn't a hit once plate P2 is excluded."),             # qa report
    ]
    done = h.answer(q_threshold, "A", "c1")
    assert done["status"] == "done"

    findings = h.drive.path("findings/findings.md").read_text()
    assert "**not a hit**" in findings
    assert "Exclude plate P2 from the analysis (confirmed by Ben Okafor" in findings
    assert "threshold: drop of at least 30% with p < 0.05" in findings
    assert "pending_approval" in findings
    assert h.drive.path("findings/notes/analysis.ipynb.md").exists()
    assert h.inbox("C")[-1] == {**h.inbox("C")[-1], "kind": "note",
                                "text": "Chen, there's a note on analysis.ipynb about plate P2."}

    # A approves the plan; the findings update.
    q_plan = next(q["id"] for q in h.questions() if q["about"].startswith("approval:"))
    assert h.answer(q_plan, "A", "approve")["status"] == "approved"
    assert "(approved)" in h.drive.path("findings/findings.md").read_text()

    # Run 2: B uploads the re-run. Nobody is asked anything.
    r2 = expected["run2"]
    stats_run2 = {"drop": round(r2["drop"], 4), "p": float(f"{r2['p']:.3g}")}
    client.replies += [
        tools(("parse_xlsx", {"path": "raw/readings_rerun.xlsx"}), ("check_consistency", {}),
              ("filter_dataset", {}),
              ("run_stats", {"compound": "ABC-12", "min_drop": 0.3, "max_p": 0.05})),
        conclusion(**stats_run2, summary="Still no evidence of an effect in this data, now with "
                                         "plates P1 and P3."),
        say(**{"pass": True, "problems": []}),
        say(message="Chen, the note on analysis.ipynb has the re-run's numbers."),
        say(message="Ana, with the re-run included it's still not a hit."),
    ]
    up = h.upload(incoming / "readings_rerun.xlsx", "B", "Re-run at 72 h with vehicle")
    assert up["runs"][0]["status"] == "done"
    assert len(h.questions()) == 3                    # no new questions in run 2
    findings = h.drive.path("findings/findings.md").read_text()
    assert "## What changed since the last run" in findings
    assert f"Drop vs vehicle: {stats_run1['drop']} → {stats_run2['drop']}" in findings
    assert client.replies == []                       # every scripted reply was used
    # The Reviewer saw the SOP, so it can check claims that cite it.
    review_call = next(c for c in client.calls if "Reviewer" in c["messages"][0]["content"])
    assert "SOP-ASSAY-004" in review_call["messages"][1]["content"]


def test_reviewer_sends_a_wrong_number_back(setup):
    """A draft whose number doesn't match stats.json is caught in code and revised."""
    h, client, expected, _ = setup
    drive = h.drive
    # Skip straight to a state where stats exist: answer everything up front.
    from harness.state import RunState
    from harness.tools.registry import call
    s = RunState(goal={"compound": "ABC-12", "readout": "x", "comparator": "vehicle",
                       "threshold": THRESHOLD})
    for t, p in [("extract_protocol_fields", "raw/protocol.pdf"), ("parse_xlsx", "raw/readings.xlsx")]:
        call(drive, s, t, {"path": p})
    call(drive, s, "check_consistency")
    drive.resolve_contradiction("c-readings-P2", "B", "exclude")
    call(drive, s, "filter_dataset")
    stats = call(drive, s, "run_stats", {"compound": "ABC-12", **THRESHOLD})

    goal = {"id": "g-001", "requester": "A", "question": "Is ABC-12 a hit?",
            "slots": s.goal, "status": "open", "runs": [], "history": []}
    h._save("goals", goal)
    client.replies += [
        conclusion(stats["drop"], stats["p"], value_drop=0.37),           # wrong number
        conclusion(stats["drop"], stats["p"],
                   summary="ABC-12 doesn't work."),                       # overclaims
        conclusion(stats["drop"], stats["p"]),                            # fixed
        say(**{"pass": True, "problems": []}),
        say(message="Done."),
    ]
    out = h._start_run(goal, "test")
    assert out["status"] == "done"
    reviews = [json.loads(m["content"])["review"] for m in h._load("runs", out["run"])["messages"]
               if m["role"] == "tool" and "review" in m["content"]]
    assert "0.37" in reviews[0]["problems"][0]
    assert "doesn't work" in reviews[1]["problems"][0]


def test_question_about_findings_goes_to_qa(setup):
    h, client, _, _ = setup
    h.drive.path("findings").mkdir(parents=True, exist_ok=True)
    h.drive.path("findings/findings.md").write_text("# Findings\nPlate P2 was excluded by Ben.")
    client.replies += [say(intent="question_about_findings", slots={}),
                       say(message="Ben excluded plate P2 because it ran 48 h.")]
    out = h.message("C", "Why was plate P2 excluded?")
    assert out["intent"] == "question_about_findings"
    assert h.inbox("C")[-1]["text"].startswith("Ben excluded")


def test_threshold_from_free_text(setup):
    h, client, _, _ = setup
    goal = {"id": "g-001", "requester": "A", "question": "Is ABC-12 a hit?",
            "slots": {"compound": "ABC-12"}, "status": "open", "runs": [], "history": []}
    h._save("goals", goal)
    # A finished run, so answering doesn't resume anything: this test is about parsing.
    run = {"id": "r-001", "goal_id": "g-001", "status": "done", "messages": [], "pending": None}
    h._save("runs", run)
    client.replies += [say(labels=["30% drop, p < 0.05"]), say(message="Hi Ana, what's a hit?")]
    [qid], errors = h._create_questions(run, goal, {"questions": [
        {"about": "slot:threshold", "text": "What counts as a hit?",
         "choices": [{"label": "30%", "value": THRESHOLD}, {"label": "bad", "value": {"min_drop": 3}}]}]})
    q = h._load("questions", qid)
    assert [c["key"] for c in q["choices"]] == ["c1", "other"]      # the invalid choice was dropped

    client.replies += [say(value={"min_drop": 0.25, "max_p": 0.01}, understood_as="25% and 0.01")]
    r = h.answer(qid, "A", "other", "a quarter drop, p under 0.01")
    assert r["recorded_as"] == "fact"
    assert h._load("goals", "g-001")["slots"]["threshold"] == {"min_drop": 0.25, "max_p": 0.01}


def test_only_qa_writes_to_people(setup):
    """Every inbox message comes from the Q&A agent: model-written, or one of its fixed
    notices. Here no provider answers at all, so only the fixed notices can appear."""
    h, client, _, _ = setup
    from harness.agents.qa import NOTICES, QA
    client.replies += [say(intent="other", slots={})]
    h.message("C", "hello")
    assert h.inbox("C")[-1] == {**h.inbox("C")[-1], "kind": "notice",
                                "text": QA.notice("not_a_request")}
    assert set(NOTICES) == {"not_a_request", "unreadable_answer", "run_stopped"}


def test_bad_labels_fall_back_to_the_fixed_format(setup):
    """A label that drops a number is rejected; after 2 tries the fixed format is used."""
    h, client, _, _ = setup
    goal = {"id": "g-001", "requester": "A", "question": "Is ABC-12 a hit?",
            "slots": {"compound": "ABC-12"}, "status": "open", "runs": [], "history": []}
    h._save("goals", goal)
    run = {"id": "r-001", "goal_id": "g-001", "status": "done", "messages": [], "pending": None}
    client.replies += [say(labels=["A big drop"]), say(labels=["A big drop"]),
                       say(message="Hi Ana, what's a hit?")]
    [qid], _ = h._create_questions(run, goal, {"questions": [
        {"about": "slot:threshold", "text": "What counts as a hit?",
         "choices": [{"label": "ignored", "value": THRESHOLD}]}]})
    assert h._load("questions", qid)["choices"][0]["label"] == "Drop of at least 30% with p < 0.05"
