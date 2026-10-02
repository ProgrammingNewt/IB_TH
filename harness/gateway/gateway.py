"""The LLM gateway: one client for every agent, with providers that can be swapped.

Providers are tried in the order `config.yaml` lists them. A provider is skipped when its
key isn't set. When a provider refuses (a content-filter stop, a policy error, or a reply
that is a refusal), the gateway moves to the next one at once and stops using the refusing
provider for the rest of the session. Timeouts, connection errors, rate limits and server
errors are retried on the same provider (5 times by default, with backoff) before moving
on, and don't bench it. Every attempt is written to the trace, and the whole run shares
one token budget.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


class TokenBudgetExceeded(Exception):
    """The run has used its token budget. The loop should stop and report."""


class GatewayError(Exception):
    """No provider could answer. `attempts` says what happened with each."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class Provider:
    name: str
    label: str
    base_url: str
    models: dict[str, str]
    key_env: str | None = None
    enabled_env: str | None = None

    def api_key(self) -> str | None:
        return os.environ.get(self.key_env) if self.key_env else "local"

    def available(self) -> tuple[bool, str]:
        if self.enabled_env and os.environ.get(self.enabled_env) != "1":
            return False, f"{self.enabled_env} is not set to 1"
        if self.key_env and not os.environ.get(self.key_env):
            return False, f"{self.key_env} is not set"
        return True, ""


@dataclass
class Completion:
    content: str
    tool_calls: list[dict]
    finish_reason: str
    provider: str
    model: str
    usage: dict
    attempts: list[dict] = field(default_factory=list)


def load_config(path: Path | None = None) -> dict:
    return yaml.safe_load((path or ROOT / "config.yaml").read_text())


RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def is_retryable(exc: Exception) -> bool:
    """Timeouts, dropped connections, rate limits and server errors: worth another try."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__
    if "Timeout" in name or "Connection" in name:
        return True
    return getattr(exc, "status_code", None) in RETRYABLE_STATUS


def _openai_client(provider: Provider):
    from openai import OpenAI
    return OpenAI(base_url=provider.base_url, api_key=provider.api_key(), timeout=60,
                  max_retries=0)          # the gateway decides what happens on failure


class Gateway:
    def __init__(self, config: dict | None = None,
                 client_factory: Callable[[Provider], Any] = _openai_client,
                 tracer: Callable[[dict], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        cfg = config or load_config()
        self.providers = [Provider(**p) for p in cfg["providers"]]
        self.agents = cfg["agents"]
        self.refusal = cfg["refusal"]
        limits = cfg.get("limits", {})
        self.token_budget = limits.get("run_token_budget", 1_000_000)
        self.retries = limits.get("retries", 5)
        self.backoff = limits.get("backoff_seconds", [1, 2, 4, 8, 16])
        self.sleep = sleep
        self.tokens_used = 0
        self.client_factory = client_factory
        self.tracer = tracer or (lambda event: None)
        self.benched: dict[str, str] = {}          # provider name -> why it was benched
        self._clients: dict[str, Any] = {}

    def _client(self, p: Provider):
        if p.name not in self._clients:
            self._clients[p.name] = self.client_factory(p)
        return self._clients[p.name]

    # ---- refusal detection ----------------------------------------------------

    def _is_refusal_reply(self, finish_reason: str, content: str, tool_calls: list) -> bool:
        if finish_reason in self.refusal["finish_reasons"]:
            return True
        if tool_calls:
            return False
        text = (content or "").strip().lower()[:300]
        return any(phrase in text for phrase in self.refusal["reply_phrases"])

    def _is_refusal_error(self, exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        text = str(exc).lower()
        return status in (400, 403) and any(w in text for w in self.refusal["error_words"])

    # ---- the call ---------------------------------------------------------------

    def _create_with_retries(self, p: Provider, kwargs: dict, event: dict):
        """Call one provider, retrying errors that are worth retrying. Raises the last error."""
        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                return self._client(p).chat.completions.create(**kwargs), started
            except Exception as exc:                                 # noqa: BLE001
                if self._is_refusal_error(exc) or not is_retryable(exc) or attempt == self.retries:
                    raise
                wait = self.backoff[min(attempt, len(self.backoff) - 1)]
                self.tracer(event | {"outcome": "retry", "attempt": attempt + 1,
                                     "reason": str(exc)[:200], "wait_s": wait})
                self.sleep(wait)
        raise AssertionError("unreachable")

    def complete(self, agent: str, messages: list[dict], tools: list[dict] | None = None,
                 json_mode: bool = False) -> Completion:
        """Call the model configured for `agent`, falling back through the providers."""
        if self.tokens_used >= self.token_budget:
            raise TokenBudgetExceeded(
                f"run used {self.tokens_used:,} of its {self.token_budget:,} token budget")
        spec = self.agents[agent]
        attempts: list[dict] = []
        for p in self.providers:
            ok, why = p.available()
            if not ok:
                attempts.append({"provider": p.name, "outcome": "skipped", "reason": why})
                continue
            if p.name in self.benched:
                attempts.append({"provider": p.name, "outcome": "skipped",
                                 "reason": f"refused earlier: {self.benched[p.name]}"})
                continue

            model = p.models[spec["tier"]]
            kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
            if spec.get("max_tokens"):
                kwargs["max_tokens"] = spec["max_tokens"]
            if tools:
                kwargs["tools"] = tools
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            event = {"agent": agent, "provider": p.name, "label": p.label, "model": model}
            started = time.perf_counter()
            try:
                resp, started = self._create_with_retries(p, kwargs, event)
            except Exception as exc:                                 # noqa: BLE001
                refused = self._is_refusal_error(exc)
                outcome = "refused" if refused else "error"
                if refused:
                    self.benched[p.name] = str(exc)[:200]
                attempts.append({"provider": p.name, "outcome": outcome, "reason": str(exc)[:200]})
                self.tracer(event | {"outcome": outcome, "reason": str(exc)[:200],
                                     "latency_ms": round((time.perf_counter() - started) * 1000)})
                continue

            choice = resp.choices[0]
            content = choice.message.content or ""
            tool_calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                          for tc in (choice.message.tool_calls or [])]
            usage = {"prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                     "completion_tokens": getattr(resp.usage, "completion_tokens", 0) or 0}
            self.tokens_used += usage["prompt_tokens"] + usage["completion_tokens"]
            latency = round((time.perf_counter() - started) * 1000)

            if self._is_refusal_reply(choice.finish_reason, content, tool_calls):
                self.benched[p.name] = f"finish_reason={choice.finish_reason}: {content[:120]}"
                attempts.append({"provider": p.name, "outcome": "refused",
                                 "reason": self.benched[p.name]})
                self.tracer(event | {"outcome": "refused", "finish_reason": choice.finish_reason,
                                     "usage": usage, "latency_ms": latency})
                continue

            attempts.append({"provider": p.name, "outcome": "ok"})
            self.tracer(event | {"outcome": "ok", "finish_reason": choice.finish_reason,
                                 "usage": usage, "latency_ms": latency,
                                 "tokens_used_in_run": self.tokens_used,
                                 "fell_back": len([a for a in attempts if a["outcome"] != "skipped"]) > 1})
            return Completion(content, tool_calls, choice.finish_reason, p.name, model, usage,
                              attempts)

        raise GatewayError(f"no provider could answer for {agent}", attempts)


def jsonl_tracer(path: Path) -> Callable[[dict], None]:
    """Append each gateway event as one JSON line, e.g. to <drive>/trace/run-1.jsonl."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)

    def write(event: dict) -> None:
        stamped = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
        with path.open("a") as fh:
            fh.write(json.dumps(stamped) + "\n")
    return write
