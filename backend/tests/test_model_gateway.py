import asyncio
import json

import httpx
import pytest


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
async def test_gateway_does_not_retry_authentication_failure(monkeypatch) -> None:
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    monkeypatch.setenv("TEST_MODEL_KEY", "secret-key")
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

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

    with pytest.raises(GatewayError, match="authentication"):
        await gateway.complete(ModelRequest(messages=[]))
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
