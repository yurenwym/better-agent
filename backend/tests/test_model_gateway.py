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

