import json

import pytest

from harness.gateway.fakes import FailingClient, RefusingClient, ScriptedClient
from harness.gateway.gateway import Gateway, GatewayError, jsonl_tracer, load_config

MSG = [{"role": "user", "content": "Is ABC-12 a hit?"}]


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("CEREBRAS_API_KEY", "test")
    monkeypatch.delenv("LOCAL_MODEL", raising=False)


def gateway(clients: dict, events: list | None = None) -> Gateway:
    return Gateway(load_config(), client_factory=lambda p: clients[p.name],
                   tracer=(events.append if events is not None else None))


def test_first_provider_answers(keys):
    one = ScriptedClient(["hello"])
    c = gateway({"model_provider_1": one, "model_provider_2": ScriptedClient([])}).complete("router", MSG)
    assert (c.provider, c.model, c.content) == ("model_provider_1", "deepseek-flash", "hello")
    assert one.calls[0]["max_tokens"] == 300 and one.calls[0]["temperature"] == 0


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


def test_error_falls_back_without_benching(keys):
    flaky = FailingClient()
    gw = gateway({"model_provider_1": flaky, "model_provider_2": ScriptedClient(["a", "b"])})
    assert gw.complete("router", MSG).provider == "model_provider_2"
    gw.complete("router", MSG)
    assert len(flaky.calls) == 2                  # timeouts don't bench a provider
    assert gw.benched == {}


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
