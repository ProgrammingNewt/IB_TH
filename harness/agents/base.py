"""Plumbing shared by the agents. Each agent still writes its own loop in its own `run`."""
from __future__ import annotations

import json
import re


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
        raise AgentReplyError(f"no JSON object in the reply: {text[:200]!r}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AgentReplyError(f"invalid JSON: {exc}") from exc


def as_user(payload) -> dict:
    content = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"role": "user", "content": content}


def fix_request(reply: str, problem: str) -> list[dict]:
    """The two messages that tell an agent what was wrong with its last reply."""
    return [{"role": "assistant", "content": reply},
            {"role": "user", "content": f"{problem} Reply again with the JSON object only."}]
