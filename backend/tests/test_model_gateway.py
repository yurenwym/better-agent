import asyncio
import json

import httpx
import pytest


@pytest.mark.parametrize("required_index", [0, 1])
def test_required_context_group_cannot_be_partially_packed(required_index):
    from app.token_budget import ContextOverflow, pack_messages_newest
    messages = [
        {"role": "system", "content": "source boundary", "_context_group": "action", "_context_required": False},
        {"role": "user", "content": "required action " * 100, "_context_group": "action", "_context_required": False},
        {"role": "user", "content": "continue"},
    ]
    messages[required_index]["_context_required"] = True
    with pytest.raises(ContextOverflow, match="required"):
        pack_messages_newest(messages, budget=200)
    result = pack_messages_newest(messages, budget=5000)
    assert [item["content"] for item in result] == [item["content"] for item in messages]
    assert all(not any(key.startswith("_context_") for key in item) for item in result)


@pytest.mark.parametrize("required_index", [0, 1])
def test_required_tool_message_keeps_call_and_result_together(required_index):
    from app.token_budget import ContextOverflow, pack_messages_newest
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "large result " * 100},
        {"role": "user", "content": "continue"},
    ]
    messages[required_index]["_context_required"] = True
    with pytest.raises(ContextOverflow):
        pack_messages_newest(messages, budget=300)
    result = pack_messages_newest(messages, budget=5000)
    assert [item["role"] for item in result] == ["assistant", "tool", "user"]


@pytest.mark.parametrize("base_url,provider_name", [("https://x/v1","deepseek"), ("https://api.deepseek.com","openai-compatible")])
def test_deepseek_request_uses_explicit_thinking_policy(base_url, provider_name):
    import httpx
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        body = 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body.encode())
    gateway = ModelGateway(ModelProfile(base_url, "deepseek-v4-flash", "KEY", provider_name=provider_name, max_attempts=1), transport=httpx.MockTransport(handler))
    import os
    os.environ["KEY"] = "x"
    import asyncio
    asyncio.run(gateway.complete(ModelRequest(messages=[], purpose="expert_researcher", thinking=False)))
    assert seen[0]["thinking"] == {"type": "disabled"}


def test_openai_compatible_request_forwards_json_response_format():
    import asyncio
    import json
    import os
    import httpx

    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        body = 'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body.encode())

    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "FORMAT_KEY", max_attempts=1),
        transport=httpx.MockTransport(handler),
    )
    os.environ["FORMAT_KEY"] = "x"
    try:
        asyncio.run(gateway.complete(ModelRequest(
            messages=[{"role": "system", "content": "return json"}],
            response_format={"type": "json_object"},
        )))
    finally:
        os.environ.pop("FORMAT_KEY", None)
    assert seen[0]["response_format"] == {"type": "json_object"}


def _sse(*payloads: dict | str) -> bytes:
    lines = []
    for payload in payloads:
        lines.append(f"data: {payload if isinstance(payload, str) else json.dumps(payload)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


@pytest.mark.asyncio
async def test_gateway_normalizes_stream_and_cache_buckets(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "demo"
        assert body["stream"] is True
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]},
                {"usage": {"input_tokens": 12, "cache_read_tokens": 4, "cache_write_tokens": 2, "output_tokens": 5, "reasoning_tokens": 2}},
            ),
        )

    gateway = ModelGateway(
        ModelProfile(
            base_url="https://provider.test/v1",
            model="demo",
            api_key_env="TEST_MODEL_KEY",
            max_attempts=4,
            network_retries=2,
        ),
        transport=httpx.MockTransport(handler),
    )

    response = await gateway.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))

    assert response.message == "hello world"
    assert response.attempts == 1
    assert response.usage.uncached_input_tokens == 8
    assert response.usage.cache_read_tokens == 4
    assert response.usage.cache_write_tokens == 2
    assert response.usage.output_tokens == 5
    assert response.usage.reasoning_tokens == 2
    assert response.timing.first_token_at is not None
    assert response.timing.finished_at is not None


@pytest.mark.asyncio
async def test_gateway_context_overflow_happens_before_transport_and_invocation(tmp_path, monkeypatch) -> None:
    """A request rejected by the local budget check must be side-effect free."""
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("OVERFLOW_MODEL_KEY", "secret-key")
    db = Database(tmp_path / "agent.db")
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("context overflow must be rejected before transport")

    gateway = ModelGateway(
        ModelProfile(
            "https://provider.test/v1",
            "demo",
            "OVERFLOW_MODEL_KEY",
            context_window=1024,
            max_output_tokens=128,
            max_attempts=3,
        ),
        transport=httpx.MockTransport(handler),
        control_store=ModelControlStore(db),
    )

    with pytest.raises(GatewayError) as caught:
        await gateway.complete(
            ModelRequest(messages=[{"role": "user", "content": "x" * 2000}]),
            context=ModelCallContext("conversation", "answer"),
        )

    assert caught.value.kind == "context_overflow"
    assert caught.value.attempts == 0
    assert calls == 0
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0


def test_pack_messages_keeps_latest_user_and_stays_within_budget() -> None:
    from app.token_budget import DEFAULT_TOKEN_COUNTER, pack_messages_newest

    messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "old" * 100},
        {"role": "assistant", "content": "answer" * 100},
        {"role": "user", "content": "current"},
    ]
    bounded = pack_messages_newest(messages, budget=180)

    assert bounded[0]["role"] == "system"
    assert bounded[-1] == {"role": "user", "content": "current"}
    assert DEFAULT_TOKEN_COUNTER.count_payload(bounded) <= 180


def test_pack_messages_drops_optional_memory_and_never_returns_over_budget() -> None:
    from app.token_budget import DEFAULT_TOKEN_COUNTER, pack_messages_newest

    memory = {
        "role": "system", "content": "memory" * 100,
        "_context_required": False, "_context_priority": 10,
        "_context_group": "memory",
    }
    messages = [
        {"role": "system", "content": "policy"},
        memory,
        {"role": "user", "content": "current"},
    ]

    bounded = pack_messages_newest(messages, budget=180)

    assert bounded == [messages[0], messages[-1]]
    assert DEFAULT_TOKEN_COUNTER.count_payload(bounded) <= 180
    assert all(not any(key.startswith("_context_") for key in message) for message in bounded)


def test_pack_messages_keeps_tool_call_and_result_atomic() -> None:
    from app.token_budget import pack_messages_newest

    assistant = {
        "role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        }],
    }
    tool = {"role": "tool", "tool_call_id": "call-1", "content": "result" * 100}
    messages = [
        {"role": "system", "content": "policy"}, assistant, tool,
        {"role": "user", "content": "current"},
    ]

    bounded = pack_messages_newest(messages, budget=180)

    assert assistant not in bounded
    assert tool not in bounded


def test_pack_messages_keeps_or_drops_an_entire_conversation_turn() -> None:
    from app.token_budget import pack_messages_newest

    old_user = {"role": "user", "content": "old question"}
    old_assistant = {"role": "assistant", "content": "large answer" * 100}
    current = {"role": "user", "content": "current"}
    bounded = pack_messages_newest([
        {"role": "system", "content": "policy"}, old_user, old_assistant, current,
    ], budget=180)

    assert old_user not in bounded
    assert old_assistant not in bounded
    assert bounded[-1] == current


def test_pack_messages_does_not_backfill_older_history_after_newer_turn_overflows() -> None:
    from app.token_budget import DEFAULT_TOKEN_COUNTER, pack_messages_newest

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "older-small-question"},
        {"role": "assistant", "content": "older-small-answer"},
        {"role": "user", "content": "newer-large-question-" + ("x" * 160)},
        {"role": "assistant", "content": "newer-large-answer-" + ("y" * 160)},
        {"role": "user", "content": "current"},
    ]
    budget = DEFAULT_TOKEN_COUNTER.count_payload([messages[0], messages[-1]]) + 100

    bounded = pack_messages_newest(messages, budget=budget)

    assert {message.get("content") for message in bounded} == {"system", "current"}


def test_pack_messages_fails_when_required_content_exceeds_budget() -> None:
    from app.token_budget import ContextOverflow, pack_messages_newest

    with pytest.raises(ContextOverflow, match="required model messages"):
        pack_messages_newest([
            {"role": "system", "content": "policy" * 100},
            {"role": "user", "content": "current"},
        ], budget=100)


@pytest.mark.asyncio
async def test_gateway_delivers_text_deltas_before_complete_returns(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")
    deltas: list[str] = []
    complete_returned = False

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]},
            ),
        )

    def on_text_delta(value: str) -> None:
        assert not complete_returned
        deltas.append(value)

    response = await ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "TEST_MODEL_KEY"),
        transport=httpx.MockTransport(handler),
    ).complete(ModelRequest(messages=[]), on_text_delta=on_text_delta)
    complete_returned = True

    assert deltas == ["hello", " world"]
    assert response.message == "hello world"


@pytest.mark.asyncio
async def test_gateway_retries_429_without_exceeding_attempt_budget(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    gateway = ModelGateway(
        ModelProfile(
            base_url="https://provider.test/v1",
            model="demo",
            api_key_env="TEST_MODEL_KEY",
            max_attempts=1,
            network_retries=2,
            retry_base_seconds=0,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception, match="retry budget exhausted"):
        await gateway.complete(ModelRequest(messages=[]))
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,kind", [(401, "authentication"), (402, "payment")])
@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic", "gemini"])
async def test_gateway_does_not_retry_authentication_failure(monkeypatch, status, kind, protocol) -> None:
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, json={"error": {"message": "private supplier detail"}})

    gateway = ModelGateway(
        ModelProfile(
            base_url="https://provider.test/v1",
            model="demo",
            api_key_env="TEST_MODEL_KEY",
            max_attempts=4,
            network_retries=2,
            provider_protocol=protocol,
        ),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GatewayError, match=kind) as caught:
        await gateway.complete(ModelRequest(messages=[]))
    assert caught.value.kind == kind
    assert attempts == 1


@pytest.mark.asyncio
async def test_gateway_reports_cancellation_without_retry(monkeypatch) -> None:
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")
    cancelled = asyncio.Event()
    cancelled.set()
    gateway = ModelGateway(
        ModelProfile(
            base_url="https://provider.test/v1",
            model="demo",
            api_key_env="TEST_MODEL_KEY",
            max_attempts=4,
        ),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"")),
    )

    with pytest.raises(GatewayError, match="cancelled"):
        await gateway.complete(ModelRequest(messages=[]), cancel_event=cancelled)


@pytest.mark.asyncio
async def test_gateway_assembles_streamed_tool_call_fragments(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        first = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "calculator", "arguments": "{\\\"expression\\\":\\\"2"}}]}, "finish_reason": None}]}
        second = {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " + 2\\\"}"}}]}, "finish_reason": "tool_calls"}]}
        return httpx.Response(200, content=_sse(first, second))

    response = await ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "TEST_MODEL_KEY"),
        transport=httpx.MockTransport(handler),
    ).complete(ModelRequest(messages=[]))

    assert response.tool_calls == [{"index": 0, "id": "call-1", "function": {"name": "calculator", "arguments": "{\\\"expression\\\":\\\"2 + 2\\\"}"}}]


@pytest.mark.asyncio
async def test_gateway_retries_one_invalid_structured_stream(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, content=b"data: {not-json}\n\ndata: [DONE]\n\n")
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    response = await ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "TEST_MODEL_KEY", retry_base_seconds=0),
        transport=httpx.MockTransport(handler),
    ).complete(ModelRequest(messages=[]))

    assert response.message == "ok"
    assert response.attempts == 2
    assert attempts == 2


@pytest.mark.asyncio
async def test_gateway_resets_stream_between_network_retry_attempts(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")
    attempts = 0
    resets: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    response = await ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "TEST_MODEL_KEY", retry_base_seconds=0),
        transport=httpx.MockTransport(handler),
    ).complete(
        ModelRequest(messages=[]),
        on_text_reset=lambda: resets.append("reset"),
    )

    assert response.message == "ok"
    assert attempts == 2
    assert resets == ["reset"]


@pytest.mark.asyncio
async def test_gateway_reports_each_real_http_attempt_when_retrying(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")
    calls = 0
    lifecycle: list[tuple[str, int, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "rate limit"}})
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}))

    gateway = ModelGateway(
        ModelProfile("https://provider.test/v1", "demo", "TEST_MODEL_KEY", max_attempts=2, network_retries=1, retry_base_seconds=0),
        transport=httpx.MockTransport(handler),
    )
    response = await gateway.complete(
        ModelRequest(messages=[]),
        on_attempt_started=lambda ordinal, reason: lifecycle.append(("started", ordinal, reason)),
        on_attempt_finished=lambda ordinal, status, error_kind, _: lifecycle.append((status, ordinal, error_kind)),
    )

    assert response.message == "ok"
    assert lifecycle == [
        ("started", 1, "primary"),
        ("failed", 1, "rate_limit"),
        ("started", 2, "retry"),
        ("succeeded", 2, None),
    ]


@pytest.mark.asyncio
async def test_anthropic_adapter_normalizes_text_tool_and_usage(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "secret-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "secret-key"
        assert body["tools"][0]["input_schema"]["type"] == "object"
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 9, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "call-1", "name": "calculator", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"expression\":\"2+2\"}"}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}},
        ]
        return httpx.Response(200, content="".join(f"event: message\ndata: {json.dumps(event)}\n\n" for event in events).encode())

    profile = ModelProfile(
        "https://api.anthropic.test/v1", "claude-test", "ANTHROPIC_TEST_KEY", provider_protocol="anthropic"
    )
    result = await ModelGateway(profile, transport=httpx.MockTransport(handler)).complete(ModelRequest(
        messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "calculator", "description": "calc", "parameters": {"type": "object"}}}],
    ))

    assert result.message == "hello"
    assert result.tool_calls[0]["id"] == "call-1"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"expression": "2+2"}
    assert result.usage.input_tokens == 9
    assert result.usage.cache_read_tokens == 2
    assert result.usage.cache_write_tokens == 1
    assert result.usage.output_tokens == 4


@pytest.mark.asyncio
async def test_gemini_adapter_normalizes_stream_tool_and_usage(monkeypatch) -> None:
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("GEMINI_TEST_KEY", "secret-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.url.path.endswith("/models/gemini-test:streamGenerateContent")
        assert request.url.params["key"] == "secret-key"
        assert body["tools"][0]["functionDeclarations"][0]["name"] == "calculator"
        chunks = [
            {"candidates": [{"content": {"parts": [{"text": "hello "}]}}]},
            {
                "candidates": [{"content": {"parts": [{"text": "world"}, {"functionCall": {"name": "calculator", "args": {"expression": "2+2"}}}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 8, "cachedContentTokenCount": 3, "candidatesTokenCount": 5},
            },
        ]
        return httpx.Response(200, content=json.dumps(chunks).encode())

    profile = ModelProfile(
        "https://generativelanguage.test/v1beta", "gemini-test", "GEMINI_TEST_KEY", provider_protocol="gemini"
    )
    result = await ModelGateway(profile, transport=httpx.MockTransport(handler)).complete(ModelRequest(
        messages=[{"role": "system", "content": "system"}, {"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "calculator", "description": "calc", "parameters": {"type": "object"}}}],
    ))

    assert result.message == "hello world"
    assert result.tool_calls[0]["function"]["name"] == "calculator"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"expression": "2+2"}
    assert result.usage.input_tokens == 8
    assert result.usage.cache_read_tokens == 3
    assert result.usage.output_tokens == 5
