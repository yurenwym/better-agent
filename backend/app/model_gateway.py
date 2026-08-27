from __future__ import annotations

import asyncio
import json
import os
import random
import time
from contextvars import ContextVar
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
    provider_protocol: str = "openai_compatible"
    provider_name: str = "openai-compatible"
    context_window: int = 0
    max_output_tokens: int = 0

    def public_view(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(os.getenv(self.api_key_env)),
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "network_retries": self.network_retries,
            "provider_protocol": self.provider_protocol,
            "provider_name": self.provider_name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    role: str | None = None
    purpose: str | None = None


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
AttemptStartedCallback = Callable[[int, str], None]
AttemptFinishedCallback = Callable[[int, str, str | None, "ModelResponse | None"], None]


class ModelGateway:
    def __init__(
        self,
        profile: ModelProfile,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        random_source: Callable[[], float] = random.random,
        control_store: Any | None = None,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self.sleep = sleep
        self.random_source = random_source
        self.control_store = control_store
        self._call_context: ContextVar[Any | None] = ContextVar("model_gateway_call_context", default=None)

    def set_call_context(self, context: Any):
        return self._call_context.set(context)

    def reset_call_context(self, token: Any) -> None:
        self._call_context.reset(token)

    async def complete(
        self,
        request: ModelRequest,
        cancel_event: asyncio.Event | None = None,
        on_text_delta: TextDeltaCallback | None = None,
        on_text_reset: TextResetCallback | None = None,
        on_attempt_started: AttemptStartedCallback | None = None,
        on_attempt_finished: AttemptFinishedCallback | None = None,
        context: Any | None = None,
    ) -> ModelResponse:
        effective_context = context or self._call_context.get()
        if self.control_store is not None:
            from dataclasses import replace
            from .model_control import ModelCallContext

            effective_context = (
                replace(
                    effective_context,
                    role=request.role or effective_context.role,
                    purpose=request.purpose or effective_context.purpose,
                )
                if effective_context is not None
                else ModelCallContext(role=request.role or "conversation", purpose=request.purpose or "complete")
            )
        handle = self.control_store.begin_invocation(self.profile, request, effective_context) if self.control_store is not None else None
        if cancel_event and cancel_event.is_set():
            if handle is not None:
                self.control_store.finish_invocation(handle, "cancelled")
            raise GatewayError("model request cancelled", "cancelled")
        api_key = os.getenv(self.profile.api_key_env)
        if not api_key:
            if handle is not None:
                self.control_store.finish_invocation(handle, "failed")
            raise GatewayError("model API key missing", "configuration")
        attempt_count = 0
        network_retry_count = 0
        structure_retry_used = False
        context_retry_used = False
        while attempt_count < max(self.profile.max_attempts, 1):
            attempt_count += 1
            reason = "primary" if attempt_count == 1 else "retry"
            if attempt_count > 1 and on_text_reset is not None:
                on_text_reset()
            if on_attempt_started is not None:
                on_attempt_started(attempt_count, reason)
            if handle is not None:
                try:
                    self.control_store.start_attempt(handle, attempt_count, reason)
                except Exception as exc:
                    from .costs import BudgetExceeded

                    if isinstance(exc, BudgetExceeded):
                        raise GatewayError(str(exc), "budget", attempt_count) from exc
                    raise
            try:
                def emit_delta(value: str) -> None:
                    if handle is not None:
                        self.control_store.mark_output_started(handle, attempt_count)
                    if on_text_delta is not None:
                        on_text_delta(value)

                response = await self._attempt(request, api_key, cancel_event, emit_delta)
                response = ModelResponse(**{**response.__dict__, "attempts": attempt_count})
                if handle is not None:
                    self.control_store.finish_attempt(handle, attempt_count, "succeeded", None, response)
                    self.control_store.finish_invocation(handle, "succeeded", attempt_count)
                if on_attempt_finished is not None:
                    on_attempt_finished(attempt_count, "succeeded", None, response)
                return response
            except GatewayError as error:
                if handle is not None:
                    self.control_store.finish_attempt(handle, attempt_count, "cancelled" if error.kind == "cancelled" else "failed", error.kind, None)
                if on_attempt_finished is not None:
                    on_attempt_finished(attempt_count, "cancelled" if error.kind == "cancelled" else "failed", error.kind, None)
                if error.kind == "cancelled":
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "cancelled")
                    raise GatewayError(str(error), error.kind, attempt_count) from error
                if error.kind == "authentication" or error.kind == "configuration":
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "failed")
                    raise GatewayError(str(error), error.kind, attempt_count) from error
                if error.kind == "structure" and not structure_retry_used:
                    structure_retry_used = True
                elif error.kind == "context_overflow" and not context_retry_used:
                    context_retry_used = True
                elif error.kind not in {"rate_limit", "server", "timeout", "provider_unavailable"}:
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "failed")
                    raise GatewayError(str(error), error.kind, attempt_count) from error
                else:
                    network_retry_count += 1
                if attempt_count >= self.profile.max_attempts:
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "failed")
                    raise GatewayError("retry budget exhausted", error.kind, attempt_count) from error
                if error.kind in {"rate_limit", "server", "timeout", "provider_unavailable"} and network_retry_count > self.profile.network_retries:
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "failed")
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
        if self.profile.provider_protocol == "anthropic":
            return await self._attempt_anthropic(request, api_key, cancel_event, on_text_delta)
        if self.profile.provider_protocol == "gemini":
            return await self._attempt_gemini(request, api_key, cancel_event, on_text_delta)
        if self.profile.provider_protocol != "openai_compatible":
            raise GatewayError("unsupported model provider protocol", "configuration")
        return await self._attempt_openai(request, api_key, cancel_event, on_text_delta)

    async def _attempt_openai(
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
        except httpx.NetworkError as exc:
            raise GatewayError("model provider unavailable", "provider_unavailable") from exc
        finished = time.perf_counter()
        return ModelResponse(
            message="".join(content),
            tool_calls=[tool_call_fragments[index] for index in sorted(tool_call_fragments)],
            finish_reason=finish_reason,
            usage=usage,
            timing=Timing(started, first_token_at, finished),
            attempts=1,
        )

    async def _attempt_anthropic(
        self,
        request: ModelRequest,
        api_key: str,
        cancel_event: asyncio.Event | None,
        on_text_delta: TextDeltaCallback | None,
    ) -> ModelResponse:
        started = time.perf_counter()
        first_token_at: float | None = None
        content: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage = UsageBuckets()
        system = "\n\n".join(str(message.get("content", "")) for message in request.messages if message.get("role") == "system")
        messages = [message for message in request.messages if message.get("role") != "system"]
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "max_tokens": request.max_tokens or 1024,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"]["parameters"],
                }
                for tool in request.tools
            ]
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.profile.timeout_seconds) as client:
                async with client.stream("POST", f"{self.profile.base_url.rstrip('/')}/messages", headers=headers, json=payload) as response:
                    await _raise_for_status(response)
                    async for line in response.aiter_lines():
                        if cancel_event and cancel_event.is_set():
                            raise GatewayError("model request cancelled", "cancelled")
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except json.JSONDecodeError as exc:
                            raise GatewayError("invalid structured model output", "structure") from exc
                        event_type = event.get("type")
                        if event_type == "message_start":
                            usage = _anthropic_usage((event.get("message") or {}).get("usage") or {}, usage)
                        elif event_type == "content_block_start":
                            block = event.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                index = int(event.get("index", len(tool_calls)))
                                tool_calls[index] = {"index": index, "id": block.get("id"), "function": {"name": block.get("name"), "arguments": ""}}
                        elif event_type == "content_block_delta":
                            delta = event.get("delta") or {}
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                token = str(delta["text"])
                                if first_token_at is None:
                                    first_token_at = time.perf_counter()
                                content.append(token)
                                if on_text_delta is not None:
                                    on_text_delta(token)
                            elif delta.get("type") == "input_json_delta":
                                index = int(event.get("index", 0))
                                tool_calls.setdefault(index, {"index": index, "function": {"arguments": ""}})["function"]["arguments"] += str(delta.get("partial_json", ""))
                        elif event_type == "message_delta":
                            finish_reason = (event.get("delta") or {}).get("stop_reason")
                            usage = _anthropic_usage(event.get("usage") or {}, usage)
        except httpx.TimeoutException as exc:
            raise GatewayError("model request timed out", "timeout") from exc
        except httpx.NetworkError as exc:
            raise GatewayError("model provider unavailable", "provider_unavailable") from exc
        return ModelResponse("".join(content), [tool_calls[key] for key in sorted(tool_calls)], finish_reason, usage, Timing(started, first_token_at, time.perf_counter()), 1)

    async def _attempt_gemini(
        self,
        request: ModelRequest,
        api_key: str,
        cancel_event: asyncio.Event | None,
        on_text_delta: TextDeltaCallback | None,
    ) -> ModelResponse:
        started = time.perf_counter()
        first_token_at: float | None = None
        content: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        finish_reason: str | None = None
        usage = UsageBuckets()
        system = "\n\n".join(str(message.get("content", "")) for message in request.messages if message.get("role") == "system")
        contents = [
            {"role": "model" if message.get("role") == "assistant" else "user", "parts": [{"text": str(message.get("content", ""))}]}
            for message in request.messages if message.get("role") != "system"
        ]
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        generation: dict[str, Any] = {}
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.max_tokens is not None:
            generation["maxOutputTokens"] = request.max_tokens
        if generation:
            payload["generationConfig"] = generation
        if request.tools:
            payload["tools"] = [{"functionDeclarations": [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "parameters": tool["function"]["parameters"],
                }
                for tool in request.tools
            ]}]
        url = f"{self.profile.base_url.rstrip('/')}/models/{self.profile.model}:streamGenerateContent"
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.profile.timeout_seconds) as client:
                response = await client.post(url, params={"key": api_key, "alt": "sse"}, json=payload)
                await _raise_for_status(response)
                if cancel_event and cancel_event.is_set():
                    raise GatewayError("model request cancelled", "cancelled")
                raw = response.content.decode("utf-8", errors="replace").strip()
                try:
                    chunks = json.loads(raw)
                    chunks = chunks if isinstance(chunks, list) else [chunks]
                except json.JSONDecodeError:
                    try:
                        chunks = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")]
                    except json.JSONDecodeError as exc:
                        raise GatewayError("invalid structured model output", "structure") from exc
                for chunk in chunks:
                    usage_payload = chunk.get("usageMetadata") or {}
                    if usage_payload:
                        usage = _gemini_usage(usage_payload)
                    for candidate in chunk.get("candidates") or []:
                        finish_reason = candidate.get("finishReason") or finish_reason
                        for part in ((candidate.get("content") or {}).get("parts") or []):
                            if part.get("text"):
                                token = str(part["text"])
                                if first_token_at is None:
                                    first_token_at = time.perf_counter()
                                content.append(token)
                                if on_text_delta is not None:
                                    on_text_delta(token)
                            if part.get("functionCall"):
                                call = part["functionCall"]
                                index = len(tool_calls)
                                tool_calls.append({
                                    "index": index,
                                    "id": f"gemini-call-{index + 1}",
                                    "function": {"name": call.get("name"), "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False, separators=(",", ":"))},
                                })
        except httpx.TimeoutException as exc:
            raise GatewayError("model request timed out", "timeout") from exc
        except httpx.NetworkError as exc:
            raise GatewayError("model provider unavailable", "provider_unavailable") from exc
        return ModelResponse("".join(content), tool_calls, finish_reason, usage, Timing(started, first_token_at, time.perf_counter()), 1)


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


def _anthropic_usage(payload: dict[str, Any], current: UsageBuckets) -> UsageBuckets:
    input_tokens = _int(payload.get("input_tokens"))
    cache_read = _int(payload.get("cache_read_input_tokens"))
    cache_write = _int(payload.get("cache_creation_input_tokens"))
    if input_tokens is None:
        uncached = current.uncached_input_tokens
        cache_read = current.cache_read_tokens
        cache_write = current.cache_write_tokens
    else:
        uncached = max(input_tokens - (cache_read or 0) - (cache_write or 0), 0)
    return UsageBuckets(
        uncached_input_tokens=uncached,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=_int(payload.get("output_tokens")) if payload.get("output_tokens") is not None else current.output_tokens,
        reasoning_tokens=current.reasoning_tokens,
    )


def _gemini_usage(payload: dict[str, Any]) -> UsageBuckets:
    input_tokens = _int(payload.get("promptTokenCount"))
    cache_read = _int(payload.get("cachedContentTokenCount"))
    return UsageBuckets(
        uncached_input_tokens=max(input_tokens - (cache_read or 0), 0) if input_tokens is not None else None,
        cache_read_tokens=cache_read,
        cache_write_tokens=0,
        output_tokens=_int(payload.get("candidatesTokenCount")),
        reasoning_tokens=_int(payload.get("thoughtsTokenCount")),
    )


async def _raise_for_status(response: httpx.Response) -> None:
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


def _int(value: Any) -> int | None:
    return int(value) if value is not None else None
