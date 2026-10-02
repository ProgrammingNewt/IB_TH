"""The LLM inside the `extract_protocol_fields` tool.

Not an agent: it never decides what happens next. The Planner calls the tool, and the
tool uses this to read free text. Every field is type-checked, and anything wrong is
replaced by the regex extractor's value.
"""
from __future__ import annotations

from harness.gateway.gateway import Gateway, GatewayError
from harness.tools.files import regex_protocol_fields
from . import prompts
from .base import AgentReplyError, as_user, fix_request, parse_json

FIELD_TYPES = {"compound": str, "readout": str, "comparator": str, "control": str,
               "treatment_hours": int, "vehicle_on_every_plate": bool, "hit_criteria": str}


def _typed(value, t):
    if t is int and isinstance(value, float) and value.is_integer():
        value = int(value)
    if value is None:
        return True, None
    ok = isinstance(value, t) and not (t is int and isinstance(value, bool))
    return ok, value


def llm_protocol_extractor(gw: Gateway, max_attempts: int = 2):
    def extract(text: str) -> dict:
        fallback = regex_protocol_fields(text)
        messages = [{"role": "system", "content": prompts.EXTRACT_PROTOCOL}, as_user(text)]
        for _ in range(max_attempts):
            try:
                reply = gw.complete("extract_protocol_fields", messages, json_mode=True).content
            except GatewayError:
                break
            try:
                out = parse_json(reply)
            except AgentReplyError as exc:
                messages += fix_request(reply, str(exc))
                continue
            fields = {}
            for k, t in FIELD_TYPES.items():
                ok, v = _typed(out.get(k), t)
                fields[k] = v if ok else fallback[k]
            if fields["vehicle_on_every_plate"] is None:
                fields["vehicle_on_every_plate"] = fallback["vehicle_on_every_plate"]
            return fields | {"extracted_by": "llm"}
        return fallback | {"extracted_by": "regex fallback"}
    return extract
