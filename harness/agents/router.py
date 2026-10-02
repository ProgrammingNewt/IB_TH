"""Router agent. One job: decide what kind of message a scientist sent."""
from __future__ import annotations

from harness.gateway.gateway import Gateway
from . import prompts
from .base import AgentReplyError, as_user, fix_request, parse_json

INTENTS = {"new_analysis", "question_about_findings", "question_about_run", "other"}
SLOTS = ("compound", "readout", "comparator")          # threshold is never taken from a message


class Router:
    def __init__(self, gateway: Gateway, max_attempts: int = 2):
        self.gw, self.max_attempts = gateway, max_attempts

    def run(self, sender: dict, text: str, active: list[dict] | None = None) -> dict:
        """Returns `{intent, slots, unclear}`. A slot is kept only if its term appears in
        the message as written (case, spaces and hyphens aside). A term the model changed,
        say a typo it "corrected", is sent back once; if it's changed again, the slot is
        dropped and listed in `unclear`, so the scientist is asked rather than assumed."""
        messages = [{"role": "system", "content": prompts.ROUTER},
                    as_user({"from": sender, "message": text, "active_analyses": active or []})]
        rewritten: dict[str, str] = {}
        intent, slots = None, {}
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
            intent = out["intent"]                      # the last valid intent seen
            raw = out.get("slots") or {}
            slots = {k: raw[k].strip() for k in SLOTS if isinstance(raw.get(k), str) and raw[k].strip()}
            rewritten = {k: v for k, v in slots.items() if _norm(v) not in _norm(text)}
            if rewritten:
                named = ", ".join(f"{k} {v!r}" for k, v in rewritten.items())
                messages += fix_request(reply, f"These aren't in the message as written: {named}. "
                                               "Copy each term exactly as the scientist typed it, typos "
                                               "included, or leave it null.")
                continue
            return {"intent": out["intent"], "slots": slots, "unclear": []}
        if rewritten and intent:   # it kept rewriting a term: keep the rest, flag those as unclear
            kept = {k: v for k, v in slots.items() if k not in rewritten}
            return {"intent": intent, "slots": kept, "unclear": sorted(rewritten)}
        return {"intent": "other", "slots": {}, "unclear": [], "note": "the Router's replies were invalid"}


def _norm(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())
