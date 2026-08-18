from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx


class GatewayError(RuntimeError):
    def __init__(self, message: str, kind: str = "unknown", attempts: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts


@dataclass(frozen=True)
class ModelProfile:
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: float = 60
    max_attempts: int = 4
    network_retries: int = 2
    retry_base_seconds: float = 1.0

    def public_view(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(os.getenv(self.api_key_env)),
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "network_retries": self.network_retries,
        }


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class UsageBuckets:
    uncached_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def input_tokens(self) -> int | None:
        values = (
            self.uncached_input_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        )
        return sum(values) if all(value is not None for value in values) else None


@dataclass(frozen=True)
class Timing:
    started_at: float
    first_token_at: float | None
    finished_at: float | None

    @property
    def ttft_seconds(self) -> float | None:
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.started_at

    @property
    def decode_seconds(self) -> float | None:
        if self.first_token_at is None or self.finished_at is None:
            return None
        return max(self.finished_at - self.first_token_at, 0.0)


@dataclass(frozen=True)
class ModelResponse:
    message: str
    tool_calls: list[dict[str, Any]]
    finish_reason: str | None
    usage: UsageBuckets
    timing: Timing
    attempts: int


Sleep = Callable[[float], Awaitable[None]]
TextDeltaCallback = Callable[[str], None]
TextResetCallback = Callable[[], None]


class ModelGateway:
    def __init__(
        self,
        profile: ModelProfile,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self.sleep = sleep
        self.random_source = random_source

    async def complete(
        self,
        request: ModelRequest,
        cancel_event: asyncio.Event | None = None,
        on_text_delta: TextDeltaCallback | None = None,
        on_text_reset: TextResetCallback | None = None,
    ) -> ModelResponse:
        if cancel_event and cancel_event.is_set():
            raise GatewayError("model request cancelled", "cancelled")
        api_key = os.getenv(self.profile.api_key_env)
        if not api_key:
            raise GatewayError("model API key missing", "configuration")
        attempt_count = 0
        network_retry_count = 0
        structure_retry_used = False
        context_retry_used = False
        while attempt_count < max(self.profile.max_attempts, 1):
            attempt_count += 1
            if attempt_count > 1 and on_text_reset is not None:
                on_text_reset()
            try:
                response = await self._attempt(request, api_key, cancel_event, on_text_delta)
                return ModelResponse(**{**response.__dict__, "attempts": attempt_count})
            except GatewayError as error:
                if error.kind == "cancelled":
                    raise GatewayError(str(error), error.kind, attempt_count) from error
                if error.kind == "authentication" or error.kind == "configuration":
                    raise GatewayError(str(error), error.kind, attempt_count) from error
                if error.kind == "structure" and not structure_retry_used:
                    structure_retry_used = True
                elif error.kind == "context_overflow" and not context_retry_used:
                    context_retry_used = True
                elif error.kind not in {"rate_limit", "server", "timeout"}:
                    raise GatewayError(str(error), error.kind, attempt_count) from error
                else:
                    network_retry_count += 1
                if attempt_count >= self.profile.max_attempts:
                    raise GatewayError("retry budget exhausted", error.kind, attempt_count) from error
                if error.kind in {"rate_limit", "server", "timeout"} and network_retry_count > self.profile.network_retries:
                    raise GatewayError("retry budget exhausted", error.kind, attempt_count) from error
                delay = self.profile.retry_base_seconds * (2 ** max(network_retry_count - 1, 0))
                if delay:
                    delay *= 0.8 + self.random_source() * 0.4
                    await self.sleep(delay)
        raise GatewayError("retry budget exhausted", "unknown", attempt_count)

    async def _attempt(
        self,
        request: ModelRequest,
        api_key: str,
        cancel_event: asyncio.Event | None,
        on_text_delta: TextDeltaCallback | None,
    ) -> ModelResponse:
        started = time.perf_counter()
        content: list[str] = []
        tool_call_fragments: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage = UsageBuckets()
        first_token_at: float | None = None
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": request.messages,
            "tools": request.tools or [],
            "stream": True,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.profile.timeout_seconds,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self.profile.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise GatewayError("model authentication failed", "authentication")
                    if response.status_code == 429:
                        raise GatewayError("model rate limited", "rate_limit")
                    if response.status_code >= 500:
                        raise GatewayError("model server error", "server")
                    if response.status_code >= 400:
                        raw = (await response.aread()).decode("utf-8", errors="replace")
                        if "context" in raw.lower() and "length" in raw.lower():
                            raise GatewayError("model context overflow", "context_overflow")
                        raise GatewayError("model request failed", "request")
                    async for line in response.aiter_lines():
                        if cancel_event and cancel_event.is_set():
                            raise GatewayError("model request cancelled", "cancelled")
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise GatewayError("invalid structured model output", "structure") from exc
                        if chunk.get("usage"):
                            usage = normalize_usage(chunk["usage"])
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            token = delta.get("content") or ""
                            if token:
                                if first_token_at is None:
                                    first_token_at = time.perf_counter()
                                content.append(token)
                                if on_text_delta is not None:
                                    on_text_delta(token)
                            if delta.get("tool_calls"):
                                for fragment in delta["tool_calls"]:
                                    index = int(fragment.get("index", len(tool_call_fragments)))
                                    current = tool_call_fragments.setdefault(index, {"index": index, "function": {"arguments": ""}})
                                    if fragment.get("id"):
                                        current["id"] = fragment["id"]
                                    function = fragment.get("function") or {}
                                    if function.get("name"):
                                        current["function"]["name"] = function["name"]
                                    if function.get("arguments"):
                                        current["function"]["arguments"] += function["arguments"]
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
        except httpx.TimeoutException as exc:
            raise GatewayError("model request timed out", "timeout") from exc
        finished = time.perf_counter()
        return ModelResponse(
            message="".join(content),
            tool_calls=[tool_call_fragments[index] for index in sorted(tool_call_fragments)],
            finish_reason=finish_reason,
            usage=usage,
            timing=Timing(started, first_token_at, finished),
            attempts=1,
        )


def normalize_usage(payload: dict[str, Any]) -> UsageBuckets:
    input_tokens = _int(payload.get("input_tokens", payload.get("prompt_tokens")))
    explicit_uncached = _int(payload.get("uncached_input_tokens"))
    cache_read = _int(payload.get("cache_read_tokens", payload.get("cached_tokens")))
    cache_write = _int(payload.get("cache_write_tokens"))
    if explicit_uncached is not None:
        uncached = explicit_uncached
    elif input_tokens is not None and cache_read is not None:
        uncached = max(input_tokens - cache_read, 0)
    else:
        uncached = None
    return UsageBuckets(
        uncached_input_tokens=uncached,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=_int(payload.get("output_tokens", payload.get("completion_tokens"))),
        reasoning_tokens=_int(payload.get("reasoning_tokens")),
    )


def _int(value: Any) -> int | None:
    return int(value) if value is not None else None
