# Reviewer

**Job:** pass or fail a draft conclusion before anyone sees it.
**Code:** `harness/agents/reviewer.py` · **Prompt:** `REVIEWER` in `harness/agents/prompts.py` · **Model:** large

**In:** the Planner's draft (verdict, summary, numbers with their sources, notes for owners, next step) and the evidence: goal slots, `stats.json`, confirmed facts, contradictions, the notebook's claims, and the SOP text.
**Out:** `{pass, problems[], by}`, where `by` says whether code or the model decided.

## Loop

```
# 1. Code checks: certain and free, so they run first
problems = []
for each number in the draft:
    open the file it cites and read the key it names
    if the file or key is missing, or the value differs by more than 1%:  add a problem
if the wording overclaims ("doesn't work", "ineffective", "proves", "no effect",
   or the compound as the subject of "causes / prevents / reverses / cures"):
    add a problem
if problems:  return fail (by: code)

# 2. Model check: does the conclusion follow from the evidence?
repeat up to 2 times:
    reply = model([REVIEWER prompt, draft + evidence], json_mode)
    if no provider answered:      return fail ("review couldn't run")
    if reply isn't JSON, or "pass" isn't true/false:  tell it, try again
    return {pass, problems}

return fail ("the Reviewer's replies were invalid")
```

## What happens after
The orchestrator decides, not the Reviewer:
- **Pass:** code renders `findings.md` from the draft, and the Q&A agent reports to the requester.
- **Fail:** the problems go back to the Planner as the reply to its `submit_conclusion`. After 2 failed revisions the run finishes anyway, and the findings list the problems that weren't resolved.
