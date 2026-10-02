"""Reviewer agent. One job: pass or fail a draft conclusion against the evidence.

Code checks run first, because they're certain and free: every number must match the
file it cites, and the wording mustn't claim more than a screen can show. Only a draft
that passes them goes to the model.
"""
from __future__ import annotations

import re
from typing import Callable

from harness.gateway.gateway import Gateway, GatewayError
from . import prompts
from .base import AgentReplyError, as_user, fix_request, parse_json

# Causal verbs are only flagged when the compound is their subject: "the 48 h run caused
# the low signal" is a fair statement.
OVERCLAIMS = [r"\bdoes(?:n't| not) work\b", r"\bineffective\b", r"\bproves?\b",
              r"\bno effect\b(?! in this data)"]
CAUSAL_VERBS = r"(?:causes?|caused|prevents?|prevented|reverses?|reversed|cures?|cured)"


def code_review(draft: dict, read_json: Callable, compound: str | None = None) -> list[str]:
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
            problems.append(f"wording '{m.group(0)}' claims more than the data shows; say "
                            "'no evidence of an effect in this data' or describe what was measured")
    return problems


class Reviewer:
    def __init__(self, gateway: Gateway, max_attempts: int = 2):
        self.gw, self.max_attempts = gateway, max_attempts

    def run(self, draft: dict, evidence: dict, read_json: Callable,
            compound: str | None = None) -> dict:
        problems = code_review(draft, read_json, compound)
        if problems:
            return {"pass": False, "problems": problems, "by": "code"}

        messages = [{"role": "system", "content": prompts.REVIEWER},
                    as_user({"draft": draft, "evidence": evidence})]
        for _ in range(self.max_attempts):
            try:
                reply = self.gw.complete("reviewer", messages, json_mode=True).content
            except GatewayError as exc:
                return {"pass": False, "problems": [f"review couldn't run: {exc}"], "by": "error"}
            try:
                out = parse_json(reply)
            except AgentReplyError as exc:
                messages += fix_request(reply, str(exc))
                continue
            if not isinstance(out.get("pass"), bool):
                messages += fix_request(reply, '"pass" must be true or false.')
                continue
            if out["pass"]:
                return {"pass": True, "problems": [], "by": "reviewer"}
            return {"pass": False, "problems": [str(p) for p in out.get("problems") or ["no reason given"]],
                    "by": "reviewer"}
        return {"pass": False, "problems": ["the Reviewer's replies were invalid"], "by": "error"}
