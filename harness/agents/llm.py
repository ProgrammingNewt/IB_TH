"""Small helpers for agents that reply in JSON."""
from __future__ import annotations

import json
import re

from harness.gateway.gateway import Gateway


class AgentReplyError(Exception):
    pass


def parse_json(text: str) -> dict:
    """Parse a JSON object, tolerating a ```json fence or text around it."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise AgentReplyError(f"no JSON object in reply: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def ask_json(gateway: Gateway, agent: str, system: str, user: str | dict) -> dict:
    """One JSON-mode call. Retries once if the reply isn't valid JSON."""
    content = user if isinstance(user, str) else json.dumps(user, indent=2, default=str)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    for attempt in range(2):
        reply = gateway.complete(agent, messages, json_mode=True)
        try:
            return parse_json(reply.content)
        except (AgentReplyError, json.JSONDecodeError) as exc:
            if attempt == 1:
                raise AgentReplyError(f"{agent} didn't return valid JSON: {exc}") from exc
            messages += [{"role": "assistant", "content": reply.content},
                         {"role": "user", "content": "That wasn't valid JSON. Reply with the JSON object only."}]
    raise AssertionError("unreachable")
