"""Planner agent. One job: decide the next step, and run drive tools until it needs
something only another agent can give.

Its loop calls the model with the tool list, runs each drive tool through the registry
(so the guardrails apply), and feeds the results back. It stops and hands control back
when the model:

* asks scientists (`ask_scientists`): the Q&A agent and the scientists take over;
* submits a conclusion (`submit_conclusion`): the Reviewer takes over;
* or can't go on (a gateway failure, the turn limit, or no tool calls after nudges).

The Planner never talks to a person and never judges its own conclusion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from harness.drive.store import Drive
from harness.gateway.gateway import Gateway, GatewayError, TokenBudgetExceeded
from harness.state import RunState
from harness.tools import registry
from . import prompts

CONTROL_TOOLS = [
    {"type": "function", "function": {
        "name": "ask_scientists",
        "description": "Ask scientists questions. The run pauses until every question is answered.",
        "parameters": {"type": "object", "required": ["questions"], "properties": {
            "questions": {"type": "array", "items": {"type": "object", "required": ["about", "text"],
                "properties": {
                    "about": {"type": "string", "description": "contradiction:<id> or slot:threshold"},
                    "text": {"type": "string", "description": "the question"},
                    "choices": {"type": "array", "items": {"type": "object",
                        "properties": {"label": {"type": "string"}, "value": {"type": "object"}}}},
                }}}}}}},
    {"type": "function", "function": {
        "name": "submit_conclusion",
        "description": "Submit the conclusion for review. Every number must cite its source.",
        "parameters": {"type": "object",
            "required": ["verdict", "summary", "numbers", "next_step"],
            "properties": {
                "verdict": {"type": "string", "enum": ["hit", "not a hit", "inconclusive"]},
                "summary": {"type": "string"},
                "numbers": {"type": "array", "items": {"type": "object",
                    "required": ["label", "value", "source", "key"],
                    "properties": {"label": {"type": "string"}, "value": {"type": "number"},
                                   "source": {"type": "string", "description": "e.g. derived/stats.json"},
                                   "key": {"type": "string", "description": "e.g. drop"}}}},
                "notes_for_owners": {"type": "array", "items": {"type": "object",
                    "properties": {"owner": {"type": "string"}, "file": {"type": "string"},
                                   "note": {"type": "string"}}}},
                "next_step": {"type": "string"},
                "plan": {"type": "string", "description": "plan file from plan_followup_plate, if any"},
            }}}},
]
CONTROL_NAMES = {t["function"]["name"] for t in CONTROL_TOOLS}


@dataclass
class Handoff:
    """Why the Planner stopped. `kind` is "ask", "submit" or "failed"."""
    kind: str
    call_id: str | None = None
    args: dict | None = None
    reason: str = ""


def tool_reply(run: dict, call_id: str, result: dict) -> None:
    run["messages"].append({"role": "tool", "tool_call_id": call_id,
                            "content": json.dumps(result, default=str)})


class Planner:
    def __init__(self, gateway: Gateway, drive: Drive, overrides: dict | None = None,
                 max_turns: int = 40, max_nudges: int = 3):
        self.gw, self.drive, self.overrides = gateway, drive, overrides or {}
        self.max_turns, self.max_nudges = max_turns, max_nudges

    @staticmethod
    def first_messages(context: str) -> list[dict]:
        return [{"role": "system", "content": prompts.PLANNER},
                {"role": "user", "content": context}]

    def run(self, run: dict, state: RunState,
            on_tool: Callable[[str, dict, dict], None]) -> Handoff:
        """Continue `run` until a handoff. `run["messages"]` is the conversation so far."""
        tools = registry.openai_tools() + CONTROL_TOOLS
        while run["turns"] < self.max_turns:
            run["turns"] += 1
            try:
                c = self.gw.complete("planner", run["messages"], tools=tools)
            except (GatewayError, TokenBudgetExceeded) as exc:
                return Handoff("failed", reason=str(exc))

            run["messages"].append({
                "role": "assistant", "content": c.content or "",
                **({"tool_calls": [{"id": t["id"], "type": "function",
                                    "function": {"name": t["name"], "arguments": t["arguments"]}}
                                   for t in c.tool_calls]} if c.tool_calls else {})})
            if not c.tool_calls:
                run["nudges"] += 1
                if run["nudges"] > self.max_nudges:
                    return Handoff("failed", reason="the Planner stopped calling tools")
                run["messages"].append({"role": "user", "content":
                                        "Call a tool, ask_scientists, or submit_conclusion."})
                continue

            handoff = None
            for t in c.tool_calls:
                try:
                    args = json.loads(t["arguments"] or "{}")
                except json.JSONDecodeError:
                    tool_reply(run, t["id"], {"ok": False, "error": "arguments weren't valid JSON"})
                    continue
                if t["name"] in CONTROL_NAMES:
                    if handoff is None:
                        handoff = Handoff("ask" if t["name"] == "ask_scientists" else "submit",
                                          t["id"], args)
                    else:
                        tool_reply(run, t["id"], {"ok": False, "error":
                                   "only one ask_scientists or submit_conclusion per turn"})
                    continue
                result = registry.call(self.drive, state, t["name"], args, self.overrides)
                run["tool_calls"] = state.tool_calls
                on_tool(t["name"], args, result)
                tool_reply(run, t["id"], result)
            if handoff:
                return handoff
        return Handoff("failed", reason=f"turn limit of {self.max_turns} reached")
