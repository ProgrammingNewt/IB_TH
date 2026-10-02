"""Q&A agent. One job: every conversation with a scientist.

It writes questions, reports results, answers questions about findings, and turns a
free-text reply into a value. It never decides anything: the Planner decides what to ask,
and the harness decides who receives it.
"""
from __future__ import annotations

from typing import Any, Callable

from harness.gateway.gateway import Gateway, GatewayError
from . import prompts
from .base import AgentReplyError, as_user, fix_request, parse_json


def _message(out: dict) -> str | None:
    m = out.get("message")
    return None if isinstance(m, str) and m.strip() else "Missing a non-empty \"message\"."


def _parsed_value(out: dict) -> str | None:
    return None if "value" in out else "Missing \"value\" (use null if the reply doesn't say)."


TASKS: dict[str, tuple[str, Callable[[dict], str | None]]] = {
    "ask": (prompts.QA_ASK, _message),
    "report": (prompts.QA_REPORT, _message),
    "answer": (prompts.QA_ANSWER, _message),
    "parse_other": (prompts.QA_PARSE_OTHER, _parsed_value),
}


class QA:
    def __init__(self, gateway: Gateway, max_attempts: int = 2):
        self.gw, self.max_attempts = gateway, max_attempts

    def run(self, task: str, payload: dict, fallback: dict) -> dict:
        """One task, with its own check. Returns `fallback` if the model can't do it."""
        system, check = TASKS[task]
        messages = [{"role": "system", "content": system}, as_user(payload)]
        for _ in range(self.max_attempts):
            try:
                reply = self.gw.complete("qa", messages, json_mode=True).content
            except GatewayError:
                return fallback
            try:
                out = parse_json(reply)
            except AgentReplyError as exc:
                messages += fix_request(reply, str(exc))
                continue
            problem = check(out)
            if problem is None:
                return out
            messages += fix_request(reply, problem)
        return fallback

    # Named entry points, so callers read clearly.

    def ask(self, person: dict, question: str, context: dict) -> str:
        return self.run("ask", {"to": person, "question": question, "context": context},
                        {"message": question})["message"].strip()

    def report(self, person: dict, findings_md: str) -> str:
        return self.run("report", {"to": person, "findings": findings_md}, {"message":
                        "The analysis is finished. The findings are in findings/findings.md."})["message"].strip()

    def answer(self, person: dict, question: str, findings_md: str) -> str:
        if not findings_md:
            return "There are no findings yet for this drive."
        return self.run("answer", {"from": person, "question": question, "findings": findings_md},
                        {"message": "I couldn't answer that just now."})["message"].strip()

    def parse_other(self, question: dict, reply: str) -> dict[str, Any]:
        example = next((c["value"] for c in question["choices"] if c.get("value")), None)
        out = self.run("parse_other", {"question": question["text"], "value_shape_example": example,
                                       "reply": reply}, {"value": None})
        return {"value": out.get("value"), "understood_as": out.get("understood_as", "")}
