# Planner

**Job:** pick the next step, run drive tools, and repeat until it needs something only another agent can give.
**Code:** `harness/agents/planner.py` · **Prompt:** `PLANNER` in `harness/agents/prompts.py` · **Model:** large

**In:** the run's conversation so far (it starts with the goal, the files and their owners, and confirmed facts) and the run state.
**Out:** a handoff, which is one of:
- `ask`: it wants to ask scientists. The Q&A agent writes the questions and the run waits.
- `submit`: it has a draft conclusion. The Reviewer takes over.
- `failed`: it can't go on (no provider answered, the token budget is used up, the turn limit was hit, or it stopped calling tools).

## Loop

```
tools = the 10 drive tools + ask_scientists + submit_conclusion

while turns < 40:
    reply = model(conversation, tools)
    if the model failed or the token budget is used up:  return failed

    add reply to conversation
    if reply has no tool calls:
        nudge "call a tool, ask_scientists or submit_conclusion"
        after 3 nudges:  return failed
        continue

    for each tool call in reply:
        if it's ask_scientists or submit_conclusion:
            remember it as the handoff (only one per turn)
        else:
            result = registry.call(tool, args)     # guardrails run first, may block
            tell the orchestrator about the result  # e.g. protocol fills goal slots
            add result to conversation

    if there's a handoff:  return it

return failed ("turn limit")
```

When it's handed back an answer or a review, the orchestrator adds it to the conversation as the reply to that tool call, and calls `run` again. The Planner picks up exactly where it stopped.

## Rules enforced in code (not in the prompt)
- Every drive tool goes through the guardrails. Analysis is blocked while a contradiction is open or a goal slot is missing, and `run_stats` must use the requester's threshold.
- Tool-call budget: 25 per run. Token budget: 1,000,000 per run.
- It never messages a person and never judges its own conclusion.
