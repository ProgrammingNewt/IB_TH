# Q&A

**Job:** every conversation with a scientist. It writes, but never decides: the Planner decides what to ask, and the orchestrator decides who receives it.
**Code:** `harness/agents/qa.py` · **Prompts:** `QA_ASK`, `QA_REPORT`, `QA_ANSWER`, `QA_PARSE_OTHER` · **Model:** small

## Tasks

| Task | In | Out | If the model fails |
|---|---|---|---|
| `ask` | the person, the Planner's question, context | a short message to that person | the Planner's own wording |
| `report` | the person, `findings.md` | 3–5 sentences on the result | "The analysis is finished..." |
| `answer` | the person, their question, `findings.md` | an answer from the findings only | "I couldn't answer that just now." |
| `parse_other` | the question, an example value, their free-text reply | `{value, understood_as}` | `{value: null}`, and the person is asked again |

Answer choices are shown as buttons, so a picked choice becomes a fact without any model involved. `parse_other` runs only when someone types "something else".

## Loop (the same for every task)

```
messages = [the task's prompt, the task's input]

repeat up to 2 times:
    reply = model(messages, json_mode)
    if no provider answered:     return the fallback
    if reply isn't JSON:         tell it, try again
    if the task's check fails:   tell it what's missing, try again
        (ask / report / answer need a non-empty "message"; parse_other needs "value")
    return reply

return the fallback
```

## Rules enforced in code
- Who receives a question is set by the orchestrator: the owner who decides for a contradiction, the requester for a goal slot or a plan approval.
- A value parsed from free text is checked before use (a threshold needs `min_drop` and `max_p` between 0 and 1). If it fails the check, nothing is recorded and the person is asked to rephrase.
