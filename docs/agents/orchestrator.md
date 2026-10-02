# Orchestrator (not an agent)

**Job:** pass work between the agents and keep state in the drive. It makes no model calls itself.
**Code:** `harness/harness.py`

## Entry points

```
message(sender, text):
    routed = Router.run(sender, text)
    if routed is a question about findings:  Q&A.answer → sender's inbox
    if routed is a new analysis:             create a goal, start a run
    otherwise:                               reply with what the harness can do

answer(question, by, choice, text?):
    contradiction → only its deciding owner's answer becomes a fact (anyone else's is a comment)
    goal slot     → only the requester can answer; "other" goes through Q&A.parse_other
    plan approval → only the requester; marks the plan approved or rejected
    then: if every question the run is waiting on is answered, resume the run

upload(file, owner, replace?, reevaluate?):
    any file type. Same name: only its owner can replace it (the old one goes to trash/)
    by default, start a new run for every goal that already has one;
    otherwise mark those goals' findings out of date

remove(file, by):
    only the file's owner. The file moves to trash/; rows, facts and open contradictions
    derived from it are dropped; findings are marked out of date. No run starts.

reevaluate(reason):
    start a new run for every goal that already has one (the UI's "Re-evaluate" button)
```

## Driving a run

```
loop(run):
    load the goal, and the run's token count so far
    repeat:
        handoff = Planner.run(run)

        failed  → tell the requester, finish the run as failed

        ask     → for each question:
                      contradiction: send to its deciding owner, copy the other owners,
                                     with the contradiction's own choices
                      goal slot:     send to the requester, keeping only choices whose value is valid
                      Q&A.ask writes the message
                  if no question was valid: tell the Planner why, continue
                  else: save the run as waiting, stop       # answer() resumes it

        submit  → review = Reviewer.run(draft, evidence)
                  pass, or 2 failed revisions used up:
                      render findings.md from the draft, write notes for file owners,
                      Q&A.report to the requester, finish
                  fail: give the problems back to the Planner, continue
```

## After each tool (hooks the Planner reports to)
- `extract_protocol_fields`: the protocol's compound, readout and comparator fill any empty goal slots, so they're never asked.
- `plan_followup_plate`: the plan is linked to the goal, and an approval question goes to the requester.
- Any tool with a `path`: the goal records whether that team file was read. Findings list every team file that wasn't, with the reason, whatever the Planner wrote.

## State in the drive
`goals/`, `runs/` (each run's whole conversation, so it can resume), `questions/`, `messages/` (inboxes), `trace/` (every model and tool call), `findings/`.
