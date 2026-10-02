"""Router, Q&A, Reviewer and the LLM protocol extractor.

Each is one JSON-mode call with its own prompt. Whatever comes back is checked in code
before anything uses it, so a bad reply can't put an unchecked value into the drive.
"""
from __future__ import annotations

import re

from harness.gateway.gateway import Gateway, GatewayError
from harness.tools.files import regex_protocol_fields
from . import prompts
from .llm import AgentReplyError, ask_json

INTENTS = {"new_analysis", "question_about_findings", "other"}


def route(gw: Gateway, sender: dict, text: str) -> dict:
    out = ask_json(gw, "router", prompts.ROUTER,
                   {"from": sender, "message": text})
    intent = out.get("intent") if out.get("intent") in INTENTS else "other"
    slots = out.get("slots") or {}
    clean = {k: slots.get(k) for k in ("compound", "readout", "comparator")
             if isinstance(slots.get(k), str) and slots.get(k).strip()}
    return {"intent": intent, "slots": clean}            # threshold is never taken from here


# ---- Q&A --------------------------------------------------------------------------

def qa_ask(gw: Gateway, person: dict, question: str, context: dict) -> str:
    try:
        out = ask_json(gw, "qa", prompts.QA_ASK,
                       {"to": person, "question": question, "context": context})
        return str(out["message"]).strip() or question
    except (AgentReplyError, GatewayError, KeyError):
        return question                                  # the Planner's own wording still works


def qa_report(gw: Gateway, person: dict, findings_md: str) -> str:
    try:
        out = ask_json(gw, "qa", prompts.QA_REPORT, {"to": person, "findings": findings_md})
        return str(out["message"]).strip()
    except (AgentReplyError, GatewayError, KeyError):
        return "The analysis is finished. The findings are in findings/findings.md."


def qa_answer(gw: Gateway, person: dict, question: str, findings_md: str) -> str:
    if not findings_md:
        return "There are no findings yet for this drive."
    out = ask_json(gw, "qa", prompts.QA_ANSWER,
                   {"from": person, "question": question, "findings": findings_md})
    return str(out.get("message", "")).strip()


def qa_parse_other(gw: Gateway, question: dict, reply: str) -> dict:
    example = next((c["value"] for c in question["choices"] if c.get("value")), None)
    out = ask_json(gw, "qa", prompts.QA_PARSE_OTHER,
                   {"question": question["text"], "value_shape_example": example, "reply": reply})
    return {"value": out.get("value"), "understood_as": out.get("understood_as", "")}


# ---- Reviewer ---------------------------------------------------------------------

# Wording that claims more than a screen can show. Causal verbs are only flagged when the
# compound is their subject: "the 48 h run caused the low signal" is a fair statement.
OVERCLAIMS = [r"\bdoes(?:n't| not) work\b", r"\bineffective\b", r"\bproves?\b",
              r"\bno effect\b(?! in this data)"]
CAUSAL_VERBS = r"(?:causes?|caused|prevents?|prevented|reverses?|reversed|cures?|cured)"


def code_review(draft: dict, read_json, compound: str | None = None) -> list[str]:
    """Checks that don't need a model: every number matches its source, no overclaiming."""
    problems = []
    for n in draft.get("numbers", []):
        src, key = n.get("source"), n.get("key")
        data = read_json(src, None) if src else None
        if not isinstance(data, dict):
            problems.append(f"number '{n.get('label')}' cites {src!r}, which isn't a JSON file in the drive")
            continue
        actual = data
        for part in str(key).split("."):
            actual = actual.get(part) if isinstance(actual, dict) else None
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            problems.append(f"number '{n.get('label')}': {src} has no numeric key {key!r}")
        elif not isinstance(n.get("value"), (int, float)) or \
                abs(actual - n["value"]) > max(1e-3, abs(actual) * 0.01):
            problems.append(f"number '{n.get('label')}' is {n.get('value')} but {src}:{key} is {actual}")
    text = " ".join([draft.get("summary", ""), draft.get("next_step", "")] +
                    [x.get("note", "") for x in draft.get("notes_for_owners", [])]).lower()
    patterns = list(OVERCLAIMS)
    if compound:
        patterns.append(re.escape(compound.lower()) + r"\W+(?:\w+\W+){0,3}?" + CAUSAL_VERBS + r"\b")
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            problems.append(f"wording '{m.group(0)}' claims more than the data shows; "
                            "say 'no evidence of an effect in this data' or describe what was measured")
    return problems


def review(gw: Gateway, draft: dict, evidence: dict, read_json,
           compound: str | None = None) -> dict:
    problems = code_review(draft, read_json, compound)
    if problems:                      # no point paying for a model review of a draft that fails these
        return {"pass": False, "problems": problems, "by": "code"}
    out = ask_json(gw, "reviewer", prompts.REVIEWER, {"draft": draft, "evidence": evidence})
    passed = out.get("pass") is True
    return {"pass": passed, "problems": [str(p) for p in out.get("problems", [])] if not passed else [],
            "by": "reviewer"}


# ---- protocol extractor -----------------------------------------------------------

FIELD_TYPES = {"compound": str, "readout": str, "comparator": str, "control": str,
               "treatment_hours": int, "vehicle_on_every_plate": bool, "hit_criteria": str}


def llm_protocol_extractor(gw: Gateway):
    """An extractor for `extract_protocol_fields` that uses the model, checked in code.

    Any field with the wrong type is replaced by the regex extractor's value, and if the
    call fails entirely the regex result is used and marked as such."""
    def extract(text: str) -> dict:
        fallback = regex_protocol_fields(text)
        try:
            out = ask_json(gw, "extract_protocol_fields", prompts.EXTRACT_PROTOCOL, text)
        except (AgentReplyError, GatewayError):
            return fallback | {"extracted_by": "regex fallback"}
        fields = {}
        for k, t in FIELD_TYPES.items():
            v = out.get(k)
            if t is int and isinstance(v, float) and v.is_integer():
                v = int(v)
            fields[k] = v if v is None or (isinstance(v, t) and not (t is int and isinstance(v, bool))) \
                else fallback[k]
        if fields["vehicle_on_every_plate"] is None:
            fields["vehicle_on_every_plate"] = fallback["vehicle_on_every_plate"]
        return fields | {"extracted_by": "llm"}
    return extract
