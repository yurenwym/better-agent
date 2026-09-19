from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Any, Protocol, Sequence

import httpx


DEFAULT_EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-4B"
DEFAULT_EMBEDDING_DIMENSIONS = 1024
QWEN3_EMBEDDING_4B_DIMENSIONS = frozenset({64, 128, 256, 512, 768, 1024, 2048})


class EmbeddingError(RuntimeError):
    pass


class EmbeddingRequestError(EmbeddingError):
    def __init__(self, message: str, *, kind: str, retryable: bool) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class EmbeddingProtocolError(EmbeddingError):
    pass


@dataclass(frozen=True)
class EmbeddingProfile:
    base_url: str
    model: str
    api_key_env: str
    dimensions: int
    timeout_seconds: float = 0.8
    provider_name: str = "siliconflow"

    def public_view(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(os.getenv(self.api_key_env)),
            "dimensions": self.dimensions,
            "timeout_seconds": self.timeout_seconds,
            "provider_name": self.provider_name,
        }


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    model: str
    dimensions: int
    prompt_tokens: int | None = None
    total_tokens: int | None = None


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> EmbeddingBatch: ...


def load_embedding_profile_from_env(*, dimensions: int | None = None) -> EmbeddingProfile:
    api_key_env = embedding_api_key_env_from_env()
    configured_key_env = _embedding_api_key_selector() or "MEMORY_EMBEDDING_API_KEY"
    if not api_key_env:
        raise ValueError(f"embedding key is not configured in {configured_key_env}")

    configured_dimensions = dimensions
    if configured_dimensions is None:
        raw_dimensions = os.getenv("MEMORY_EMBEDDING_DIMENSIONS") or os.getenv(
            "EMBEDDING_DIMENSIONS", str(DEFAULT_EMBEDDING_DIMENSIONS)
        )
        try:
            configured_dimensions = int(raw_dimensions)
        except ValueError as exc:
            raise ValueError("embedding dimensions must be an integer") from exc

    model = (os.getenv("MEMORY_EMBEDDING_MODEL") or os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)).strip()
    if not model:
        raise ValueError("embedding model is required")
    if (
        model == DEFAULT_EMBEDDING_MODEL
        and configured_dimensions not in QWEN3_EMBEDDING_4B_DIMENSIONS
    ):
        raise ValueError("embedding dimensions are unsupported by Qwen3-Embedding-4B")
    if configured_dimensions <= 0 or configured_dimensions > 2000:
        raise ValueError("embedding dimensions must be between 1 and 2000")

    try:
        timeout_seconds = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", os.getenv("MEMORY_EMBEDDING_TIMEOUT_SECONDS", "0.8")))
    except ValueError as exc:
        raise ValueError("embedding timeout must be a number") from exc
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("embedding timeout must be positive and finite")

    base_url = os.getenv("EMBEDDING_BASE_URL", os.getenv(
        "MEMORY_EMBEDDING_BASE_URL", DEFAULT_EMBEDDING_BASE_URL
    )).strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("embedding base URL must use HTTP or HTTPS")

    return EmbeddingProfile(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        dimensions=configured_dimensions,
        timeout_seconds=timeout_seconds,
        provider_name=os.getenv("MEMORY_EMBEDDING_PROVIDER_NAME", "siliconflow").strip()
        or "siliconflow",
    )


def embedding_api_key_env_from_env() -> str | None:
    """Return the explicitly usable embedding credential reference, if any."""
    api_key_env = _embedding_api_key_selector()
    return api_key_env if api_key_env and os.getenv(api_key_env) else None


def _embedding_api_key_selector() -> str | None:
    explicit_embedding = any(os.getenv(name) for name in (
        "EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS",
    ))
    api_key_env = "MEMORY_EMBEDDING_API_KEY" if os.getenv("MEMORY_EMBEDDING_API_KEY") else None
    if not api_key_env and explicit_embedding:
        api_key_env = os.getenv("EMBEDDING_API_KEY_ENV")
    if not api_key_env and explicit_embedding:
        api_key_env = os.getenv("MEMORY_EMBEDDING_API_KEY_ENV")
    if api_key_env and api_key_env.startswith("AGENT_FALLBACK_MODEL_"):
        return None
    if not api_key_env:
        api_key_env = "EMBEDDING_API_KEY" if os.getenv("EMBEDDING_API_KEY") else "MEMORY_EMBEDDING_API_KEY"
    api_key_env = api_key_env.strip()
    return api_key_env or "MEMORY_EMBEDDING_API_KEY"


class OpenAICompatibleEmbeddingClient:
    def __init__(
        self,
        profile: EmbeddingProfile,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self._client = httpx.Client(
            transport=transport,
            timeout=profile.timeout_seconds,
            headers={"Content-Type": "application/json"},
        )

    def __enter__(self) -> OpenAICompatibleEmbeddingClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        normalized = self._validate_inputs(texts)
        api_key = os.getenv(self.profile.api_key_env)
        if not api_key:
            raise EmbeddingRequestError(
                "embedding credential is unavailable", kind="configuration", retryable=False
            )

        try:
            response = self._client.post(
                f"{self.profile.base_url}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": self.profile.model,
                    "input": normalized,
                    "encoding_format": "float",
                    "dimensions": self.profile.dimensions,
                },
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise EmbeddingRequestError(
                "embedding request timed out", kind="timeout", retryable=True
            ) from exc
        except httpx.RequestError as exc:
            raise EmbeddingRequestError(
                "embedding network request failed", kind="network", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            kind = "rate_limit" if status == 429 else "server" if status >= 500 else "request"
            raise EmbeddingRequestError(
                f"embedding provider returned HTTP {status}",
                kind=kind,
                retryable=status == 429 or status >= 500,
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise EmbeddingProtocolError("embedding response is not valid JSON") from exc
        return self._decode(payload, expected_count=len(normalized))

    @staticmethod
    def _validate_inputs(texts: Sequence[str]) -> list[str]:
        if isinstance(texts, (str, bytes)) or not texts:
            raise ValueError("embedding input must be a non-empty sequence of strings")
        normalized: list[str] = []
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("embedding input contains an empty or non-string item")
            normalized.append(text)
        return normalized

    def _decode(self, payload: Any, *, expected_count: int) -> EmbeddingBatch:
        if not isinstance(payload, dict):
            raise EmbeddingProtocolError("embedding response must be an object")
        if payload.get("model") != self.profile.model:
            raise EmbeddingProtocolError("embedding response model does not match profile")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected_count:
            raise EmbeddingProtocolError("embedding response count does not match input")

        by_index: dict[int, tuple[float, ...]] = {}
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                raise EmbeddingProtocolError("embedding response indexes are invalid")
            index = item["index"]
            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list):
                raise EmbeddingProtocolError("embedding vector must be a float array")
            if len(raw_vector) != self.profile.dimensions:
                raise EmbeddingProtocolError("embedding vector dimensions do not match profile")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_vector):
                raise EmbeddingProtocolError("embedding vector must contain numbers")
            vector = tuple(float(value) for value in raw_vector)
            if not all(math.isfinite(value) for value in vector):
                raise EmbeddingProtocolError("embedding vector must contain finite values")
            if not any(value != 0 for value in vector):
                raise EmbeddingProtocolError("embedding vector must not be zero for cosine search")
            if index in by_index:
                raise EmbeddingProtocolError("embedding response indexes are invalid")
            by_index[index] = vector

        if set(by_index) != set(range(expected_count)):
            raise EmbeddingProtocolError("embedding response indexes are invalid")

        usage = payload.get("usage")
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        return EmbeddingBatch(
            vectors=tuple(by_index[index] for index in range(expected_count)),
            model=self.profile.model,
            dimensions=self.profile.dimensions,
            prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
            total_tokens=total_tokens if isinstance(total_tokens, int) else None,
        )
