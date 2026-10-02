# Protocol extractor (an LLM inside a tool, not an agent)

**Job:** turn a protocol's free text into fixed fields. It runs inside the `extract_protocol_fields` tool, which the Planner calls. It never decides what happens next.
**Code:** `harness/agents/extractor.py` · **Prompt:** `EXTRACT_PROTOCOL` in `harness/agents/prompts.py` · **Model:** small

**In:** the PDF's text, which the tool extracts in code.
**Out:** `compound, readout, comparator, control, treatment_hours, vehicle_on_every_plate, hit_criteria`, plus `extracted_by: llm | regex fallback`.

## Loop

```
fallback = regex_protocol_fields(text)        # works with no model at all

repeat up to 2 times:
    reply = model([EXTRACT_PROTOCOL prompt, text], json_mode)
    if no provider answered:  stop
    if reply isn't JSON:      tell it, try again
    for each field:
        right type (or null)? keep it, otherwise use the regex value
    return fields (extracted_by: llm)

return fallback (extracted_by: regex fallback)
```
