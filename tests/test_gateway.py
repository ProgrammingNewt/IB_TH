import json

import pytest

from harness.gateway.fakes import BadKeyClient, FailingClient, RefusingClient, ScriptedClient
from harness.gateway.gateway import (Gateway, GatewayError, TokenBudgetExceeded, jsonl_tracer,
                                     load_config)

MSG = [{"role": "user", "content": "Is ABC-12 a hit?"}]


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("CEREBRAS_API_KEY", "test")
    monkeypatch.delenv("LOCAL_MODEL", raising=False)


def gateway(clients: dict, events: list | None = None, waits: list | None = None,
            config: dict | None = None) -> Gateway:
    return Gateway(config or load_config(), client_factory=lambda p: clients[p.name],
                   tracer=(events.append if events is not None else None),
                   sleep=(waits.append if waits is not None else lambda s: None))


def test_first_provider_answers(keys):
    one = ScriptedClient(["hello"])
    c = gateway({"model_provider_1": one, "model_provider_2": ScriptedClient([])}).complete("router", MSG)
    assert (c.provider, c.model, c.content) == ("model_provider_1", "deepseek-flash", "hello")
    assert one.calls[0]["temperature"] == 0
    assert "max_tokens" not in one.calls[0]       # each provider uses its own maximum


@pytest.mark.parametrize("style", ["reply", "content_filter", "error"])
def test_refusal_falls_back_and_benches_the_provider(keys, style):
    events = []
    refusing = RefusingClient(style)
    second = ScriptedClient(["answer 1", "answer 2"])
    gw = gateway({"model_provider_1": refusing, "model_provider_2": second}, events)

    c = gw.complete("planner", MSG)
    assert c.provider == "model_provider_2" and c.model == "gpt-oss-120b"
    assert [a["outcome"] for a in c.attempts] == ["refused", "ok"]
    assert events[0]["outcome"] == "refused" and events[-1]["fell_back"] is True

    # The refusing provider isn't asked again this session.
    c2 = gw.complete("planner", MSG)
    assert len(refusing.calls) == 1
    assert c2.attempts[0]["outcome"] == "skipped"
    assert c2.attempts[0]["reason"].startswith("refused earlier")
    assert "model_provider_1" in gw.benched


def test_timeouts_retry_5_times_then_fall_back_without_benching(keys):
    down = FailingClient()
    waits = []
    gw = gateway({"model_provider_1": down, "model_provider_2": ScriptedClient(["a", "b"])},
                 waits=waits)
    assert gw.complete("router", MSG).provider == "model_provider_2"
    assert len(down.calls) == 6                   # the first try plus 5 retries
    assert waits == [1, 2, 4, 8, 16]
    gw.complete("router", MSG)
    assert len(down.calls) == 12                  # a timeout doesn't bench the provider
    assert gw.benched == {}


def test_a_timeout_that_recovers_stays_on_the_same_provider(keys):
    events, waits = [], []
    flaky = FailingClient(times=2, then="made it")
    c = gateway({"model_provider_1": flaky, "model_provider_2": ScriptedClient([])},
                events, waits).complete("planner", MSG)
    assert (c.provider, c.content) == ("model_provider_1", "made it")
    assert waits == [1, 2]
    assert [e["outcome"] for e in events] == ["retry", "retry", "ok"]


def test_bad_key_is_not_retried(keys):
    bad = BadKeyClient()
    waits = []
    gw = gateway({"model_provider_1": bad, "model_provider_2": ScriptedClient(["x"])},
                 waits=waits)
    assert gw.complete("router", MSG).provider == "model_provider_2"
    assert len(bad.calls) == 1 and waits == []
    assert gw.benched == {}                       # a bad key isn't a refusal


def test_refusals_are_not_retried(keys):
    refusing = RefusingClient("error")
    waits = []
    gateway({"model_provider_1": refusing, "model_provider_2": ScriptedClient(["x"])},
            waits=waits).complete("router", MSG)
    assert len(refusing.calls) == 1 and waits == []


def test_run_token_budget(keys):
    cfg = load_config()
    cfg["limits"]["run_token_budget"] = 250       # each fake reply uses 120 tokens
    gw = gateway({"model_provider_1": ScriptedClient(["a", "b", "c"]),
                  "model_provider_2": ScriptedClient([])}, config=cfg)
    gw.complete("router", MSG)
    gw.complete("router", MSG)
    assert gw.tokens_used == 240
    gw.complete("router", MSG)                    # 240 < 250, so this call still goes out
    with pytest.raises(TokenBudgetExceeded):
        gw.complete("router", MSG)


def test_default_budget_is_one_million():
    assert load_config()["limits"]["run_token_budget"] == 1_000_000


def test_missing_key_is_skipped(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("CEREBRAS_API_KEY", "test")
    c = gateway({"model_provider_2": ScriptedClient(["x"])}).complete("qa", MSG)
    assert c.provider == "model_provider_2"
    assert c.attempts[0] == {"provider": "model_provider_1", "outcome": "skipped",
                             "reason": "DEEPSEEK_API_KEY is not set"}


def test_local_model_is_the_last_resort(keys, monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL", "1")
    c = gateway({"model_provider_1": RefusingClient(), "model_provider_2": RefusingClient(),
                 "local_model": ScriptedClient(["local answer"])}).complete("router", MSG)
    assert (c.provider, c.model) == ("local_model", "qwen2.5:7b")


def test_all_refuse_raises_with_every_attempt(keys):
    with pytest.raises(GatewayError) as err:
        gateway({"model_provider_1": RefusingClient(),
                 "model_provider_2": RefusingClient()}).complete("router", MSG)
    outcomes = [a["outcome"] for a in err.value.attempts]
    assert outcomes == ["refused", "refused", "skipped"]


def test_tool_calls_pass_through_and_are_never_a_refusal(keys):
    reply = {"content": "I can't help with that alone, so I'll call a tool.",
             "tool_calls": [{"name": "parse_xlsx", "arguments": '{"path": "raw/readings.xlsx"}'}],
             "finish_reason": "tool_calls"}
    client = ScriptedClient([reply])
    tools = [{"type": "function", "function": {"name": "parse_xlsx", "parameters": {}}}]
    c = gateway({"model_provider_1": client, "model_provider_2": ScriptedClient([])}).complete(
        "planner", MSG, tools=tools)
    assert c.tool_calls[0]["name"] == "parse_xlsx"
    assert client.calls[0]["tools"] == tools


def test_json_mode_and_trace_file(keys, tmp_path):
    path = tmp_path / "trace" / "run-1.jsonl"
    client = ScriptedClient(['{"intent": "new_analysis"}'])
    gw = Gateway(load_config(), client_factory=lambda p: client, tracer=jsonl_tracer(path))
    gw.complete("router", MSG, json_mode=True)
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    event = json.loads(path.read_text().splitlines()[0])
    assert event["agent"] == "router" and event["outcome"] == "ok"
    assert event["usage"] == {"prompt_tokens": 100, "completion_tokens": 20}
