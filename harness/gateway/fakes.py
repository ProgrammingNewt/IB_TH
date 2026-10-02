"""Fake provider clients with the same shape as the OpenAI SDK's, for tests and demos.

`RefusingClient` is what the "provider bans biology" eval uses: swap it in for
model_provider_1 and the run should finish on model_provider_2 with the same result.
"""
from __future__ import annotations

from types import SimpleNamespace as NS


def _response(content: str = "", tool_calls: list[dict] | None = None,
              finish_reason: str = "stop"):
    calls = [NS(id=c.get("id", f"call_{i}"), type="function",
                function=NS(name=c["name"], arguments=c["arguments"]))
             for i, c in enumerate(tool_calls or [])]
    message = NS(content=content, tool_calls=calls or None)
    return NS(choices=[NS(message=message, finish_reason=finish_reason)],
              usage=NS(prompt_tokens=100, completion_tokens=20))


class _Client:
    def __init__(self):
        self.calls: list[dict] = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.respond(kwargs)

    def respond(self, kwargs):
        raise NotImplementedError


class ScriptedClient(_Client):
    """Returns the given replies in order. A reply is a string or a dict with
    `content`, `tool_calls` and `finish_reason`."""

    def __init__(self, replies: list):
        super().__init__()
        self.replies = list(replies)

    def respond(self, kwargs):
        reply = self.replies.pop(0)
        return _response(reply) if isinstance(reply, str) else _response(**reply)


class RefusingClient(_Client):
    """Refuses everything, the way a provider that bans biology topics would."""

    def __init__(self, style: str = "reply"):
        super().__init__()
        self.style = style

    def respond(self, kwargs):
        if self.style == "content_filter":
            return _response("", finish_reason="content_filter")
        if self.style == "error":
            raise PolicyError()
        return _response("I can't help with requests about biological agents.")


class PolicyError(Exception):
    status_code = 400

    def __str__(self):
        return "Error code: 400 - request blocked by content policy"


class FailingClient(_Client):
    """Fails like a network problem: not a refusal, just unavailable."""

    def respond(self, kwargs):
        raise TimeoutError("request timed out")
