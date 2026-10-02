"""Router agent. One job: decide what kind of message a scientist sent."""
from __future__ import annotations

from harness.gateway.gateway import Gateway
from . import prompts
from .base import AgentReplyError, as_user, fix_request, parse_json

INTENTS = {"new_analysis", "question_about_findings", "other"}
SLOTS = ("compound", "readout", "comparator")          # threshold is never taken from a message


class Router:
    def __init__(self, gateway: Gateway, max_attempts: int = 2):
        self.gw, self.max_attempts = gateway, max_attempts

    def run(self, sender: dict, text: str) -> dict:
        messages = [{"role": "system", "content": prompts.ROUTER},
                    as_user({"from": sender, "message": text})]
        for _ in range(self.max_attempts):
            reply = self.gw.complete("router", messages, json_mode=True).content
            try:
                out = parse_json(reply)
            except AgentReplyError as exc:
                messages += fix_request(reply, str(exc))
                continue
            if out.get("intent") not in INTENTS:
                messages += fix_request(reply, f"intent must be one of {sorted(INTENTS)}.")
                continue
            slots = out.get("slots") or {}
            return {"intent": out["intent"],
                    "slots": {k: slots[k].strip() for k in SLOTS
                              if isinstance(slots.get(k), str) and slots[k].strip()}}
        return {"intent": "other", "slots": {}, "note": "the Router's replies were invalid"}
