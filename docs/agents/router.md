# Router

**Job:** decide what kind of message a scientist sent.
**Code:** `harness/agents/router.py` · **Prompt:** `ROUTER` in `harness/agents/prompts.py` · **Model:** small

**In:** who sent it, and the message text.
**Out:** `{intent, slots}`. `intent` is one of `new_analysis`, `question_about_findings` or `other`. `slots` holds whatever the message itself states (compound, readout, comparator).

## Loop

```
messages = [ROUTER prompt, {from: sender, message: text}]

repeat up to 2 times:
    reply = model(messages, json_mode)
    if reply isn't JSON:            tell it, try again
    if intent isn't one of the 3:   tell it, try again
    keep only compound / readout / comparator from slots
    return {intent, slots}

return {intent: "other"}            # couldn't get a valid reply
```

## Rules enforced in code
- The threshold is never taken from a message, even if the model fills it in. Thresholds come only from the requester's answer to a question.
- Any slot that isn't a non-empty string is dropped.
