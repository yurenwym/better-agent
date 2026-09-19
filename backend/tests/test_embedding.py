from __future__ import annotations

import json
import math

import httpx
import pytest


def test_embedding_profile_defaults_to_qwen_4b_1024_without_exposing_secret(monkeypatch) -> None:
    from app.embedding import load_embedding_profile_from_env

    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "test-secret-value")

    profile = load_embedding_profile_from_env()

    assert profile.base_url == "https://api.siliconflow.cn/v1"
    assert profile.model == "Qwen/Qwen3-Embedding-4B"
    assert profile.dimensions == 1024
    assert profile.api_key_env == "MEMORY_EMBEDDING_API_KEY"
    assert profile.timeout_seconds == pytest.approx(0.8)
    public = profile.public_view()
    assert public["api_key_configured"] is True
    assert public["api_key_env"] == "MEMORY_EMBEDDING_API_KEY"
    assert "test-secret-value" not in json.dumps(public)


def test_embedding_profile_requires_secret_and_valid_dimensions(monkeypatch) -> None:
    from app.embedding import load_embedding_profile_from_env

    monkeypatch.delenv("MEMORY_EMBEDDING_API_KEY", raising=False)
    # Verify the default missing-key error independently of desktop selectors.
    for name in ("EMBEDDING_API_KEY_ENV", "MEMORY_EMBEDDING_API_KEY_ENV", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="MEMORY_EMBEDDING_API_KEY"):
        load_embedding_profile_from_env()

    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "configured")
    monkeypatch.setenv("MEMORY_EMBEDDING_DIMENSIONS", "2")
    with pytest.raises(ValueError, match="dimensions"):
        load_embedding_profile_from_env()


def test_embedding_client_posts_openai_compatible_request_in_input_order(monkeypatch) -> None:
    from app.embedding import OpenAICompatibleEmbeddingClient, load_embedding_profile_from_env

    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "test-secret-value")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "Qwen/Qwen3-Embedding-4B",
                "data": [
                    {"object": "embedding", "index": 1, "embedding": [0.0] * 63 + [1.0]},
                    {"object": "embedding", "index": 0, "embedding": [1.0] + [0.0] * 63},
                ],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )

    profile = load_embedding_profile_from_env(dimensions=64)
    with OpenAICompatibleEmbeddingClient(
        profile, transport=httpx.MockTransport(handler)
    ) as client:
        batch = client.embed(["first", "second"])

    assert seen == {
        "url": "https://api.siliconflow.cn/v1/embeddings",
        "authorization": "Bearer test-secret-value",
        "body": {
            "model": "Qwen/Qwen3-Embedding-4B",
            "input": ["first", "second"],
            "encoding_format": "float",
            "dimensions": 64,
        },
    }
    assert batch.vectors == (
        (1.0, *([0.0] * 63)),
        (*([0.0] * 63), 1.0),
    )
    assert batch.model == profile.model
    assert batch.dimensions == 64


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            {
                "model": "another-model",
                "data": [{"index": 0, "embedding": [1.0] + [0.0] * 63}],
            },
            "model",
        ),
        (
            {
                "model": "Qwen/Qwen3-Embedding-4B",
                "data": [{"index": 0, "embedding": [1.0] * 63}],
            },
            "dimensions",
        ),
        (
            {
                "model": "Qwen/Qwen3-Embedding-4B",
                "data": [{"index": 0, "embedding": [math.nan] + [0.0] * 63}],
            },
            "finite",
        ),
        (
            {
                "model": "Qwen/Qwen3-Embedding-4B",
                "data": [{"index": 1, "embedding": [1.0] + [0.0] * 63}],
            },
            "indexes",
        ),
    ],
)
def test_embedding_client_rejects_vectors_from_the_wrong_space(
    monkeypatch, response: dict, error: str
) -> None:
    from app.embedding import (
        EmbeddingProtocolError,
        OpenAICompatibleEmbeddingClient,
        load_embedding_profile_from_env,
    )

    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "test-secret-value")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(response, allow_nan=True).encode(),
            headers={"content-type": "application/json"},
        )

    profile = load_embedding_profile_from_env(dimensions=64)
    with OpenAICompatibleEmbeddingClient(
        profile, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(EmbeddingProtocolError, match=error):
            client.embed(["text"])


def test_embedding_client_classifies_http_failure_without_leaking_secret(monkeypatch) -> None:
    from app.embedding import (
        EmbeddingRequestError,
        OpenAICompatibleEmbeddingClient,
        load_embedding_profile_from_env,
    )

    secret = "test-secret-value"
    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", secret)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": f"rate limited {secret}"})

    profile = load_embedding_profile_from_env(dimensions=64)
    with OpenAICompatibleEmbeddingClient(
        profile, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(EmbeddingRequestError) as caught:
            client.embed(["text"])

    assert caught.value.kind == "rate_limit"
    assert secret not in str(caught.value)


def test_embedding_client_normalizes_transport_failure(monkeypatch) -> None:
    from app.embedding import (
        EmbeddingRequestError,
        OpenAICompatibleEmbeddingClient,
        load_embedding_profile_from_env,
    )

    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "test-secret-value")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("connection closed", request=request)

    profile = load_embedding_profile_from_env(dimensions=64)
    with OpenAICompatibleEmbeddingClient(
        profile, transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(EmbeddingRequestError) as caught:
            client.embed(["text"])

    assert caught.value.kind == "network"
    assert caught.value.retryable is True
