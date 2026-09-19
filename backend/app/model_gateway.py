from __future__ import annotations

import asyncio
import json
import os
import random
import time
from urllib.parse import urlsplit
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx


NON_RECOVERABLE_ERRORS = {"cancelled", "budget", "authentication", "configuration", "payment"}


class GatewayError(RuntimeError):
    def __init__(self, message: str, kind: str = "unknown", attempts: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts
        if kind == "payment":
            self.public_message = "模型服务余额不足或需要付款，本次请求已停止。请检查供应商账户后重试。"
        elif kind == "budget":
            self.public_message = (
                "当前模型缺少计费价格配置，费用检查已阻止请求发送。请先补齐模型价格；重复发送不会解决。"
                if "price is unavailable" in message else
                "本次请求被预算限制阻止。请检查费用限额、调用次数或任务期限后再继续。"
            )


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
    context_window: int = 32768
    max_output_tokens: int = 4096
    registered_profile_version_id: str | None = None
    declared_capabilities: frozenset[str] | None = None
    # --- R1 budget contract (see token_budget.effective_input_budget) ---
    # admitted_context_limit (A) is the evidenced working capacity. It is not
    # optional for a tier-A profile, and an unverified repository default may
    # never stand in for it.
    admitted_context_limit: int | None = None
    soft_context_limit: int | None = None
    context_window_verified: bool = False
    validation_tier: str | None = None
    counter_id: str = "utf8-upper-bound"
    counter_version: str = "utf8-upper-bound-v1"
    counter_evidence_version: str | None = None
    capacity_evidence: str | None = None
    protocol_budget: Any = None
    # --- T02 working-window contract (see model_capacity.resolve_working_window) ---
    # ``working_window_mode`` is "auto", "manual" or None (legacy profile that
    # predates the contract). The resolved values are recorded so a frozen
    # profile can explain why its effective window is what it is.
    working_window_mode: str | None = None
    model_context_limit: int | None = None
    model_max_output_limit: int | None = None
    capacity_status: str | None = None
    capacity_source: str | None = None
    counter_mode: str | None = None
    # --- R1/R2 selection policy (see token_budget.history_min_turns /
    # compact_target / recent_window_budget) ---
    # Ratios and turn counts only. The hot-window selector must never carry an
    # absolute byte constant, so every knob is versioned profile policy.
    history_min_turns: int | None = None
    compact_ratio: float | None = None
    # R2-02: N is a floor on the count, R is a ceiling on the bytes.
    recent_window_bytes: int | None = None
    recent_window_ratio: float | None = None
    # R2-03: the static early-archival line and reserve, as ratios of H.
    # ``archive_prefix_reserve`` is a declared byte reservation for the leading
    # non-history context and defaults to 0 (see token_budget).
    archive_trigger_ratio: float | None = None
    archive_reserve_ratio: float | None = None
    archive_prefix_reserve: int | None = None

    def public_view(self) -> dict[str, Any]:
        from .config import resolve_credential
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(resolve_credential(self.api_key_env)),
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "network_retries": self.network_retries,
            "provider_protocol": self.provider_protocol,
            "provider_name": self.provider_name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "admitted_context_limit": self.admitted_context_limit,
            "soft_context_limit": self.soft_context_limit,
            "context_window_verified": self.context_window_verified,
            "validation_tier": self.validation_tier,
            "counter_id": self.counter_id,
            "counter_version": self.counter_version,
            "counter_evidence_version": self.counter_evidence_version,
            "capacity_evidence": self.capacity_evidence,
            "working_window_mode": self.working_window_mode,
            "model_context_limit": self.model_context_limit,
            "model_max_output_limit": self.model_max_output_limit,
            "capacity_status": self.capacity_status,
            "capacity_source": self.capacity_source,
            "counter_mode": self.counter_mode,
            "history_min_turns": self.history_min_turns,
            "compact_ratio": self.compact_ratio,
            "recent_window_bytes": self.recent_window_bytes,
            "recent_window_ratio": self.recent_window_ratio,
            "archive_trigger_ratio": self.archive_trigger_ratio,
            "archive_reserve_ratio": self.archive_reserve_ratio,
            "archive_prefix_reserve": self.archive_prefix_reserve,
        }


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    role: str | None = None
    purpose: str | None = None
    thinking: bool | None = None
    response_format: dict[str, Any] | None = None
    single_attempt: bool = False


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
OutputStartedCallback = Callable[[str], None]


class ModelGateway:
    def __init__(
        self,
        profile: ModelProfile,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        random_source: Callable[[], float] = random.random,
        control_store: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.profile = profile
        self.transport = transport
        self.sleep = sleep
        self.random_source = random_source
        self.control_store = control_store
        self.http_client = http_client
        self._call_context: ContextVar[Any | None] = ContextVar("model_gateway_call_context", default=None)

    def set_call_context(self, context: Any):
        return self._call_context.set(context)

    def reset_call_context(self, token: Any) -> None:
        self._call_context.reset(token)

    def input_limit(self, **_: Any) -> int:
        from .token_budget import request_budget
        return request_budget(self.profile).input_limit

    def packing_limit(self, message_count: int = 0, tool_count: int = 0, **_: Any) -> int:
        """``input_limit`` minus the adapter overhead for a request this size.

        The selector counts ``messages``/``tools``; the wire gate counts the
        finished body. Reserving the measured difference keeps them consistent.
        """
        from .token_budget import packing_limit as _packing_limit
        return _packing_limit(self.profile, message_count=message_count, tool_count=tool_count)

    def request_counter(self, **_: Any):
        """The counter this gateway's profile will actually use.

        The packing layer and the send gate must share one ruler, so callers
        pass this counter to ``pack_messages_newest`` instead of relying on the
        global default.
        """
        from .token_budget import counter_for_profile
        return counter_for_profile(self.profile).counter

    @staticmethod
    def _provider_messages(request: ModelRequest) -> list[dict[str, Any]]:
        """The messages that may legally reach a provider.

        ``_context_*`` keys are local packing metadata. They are stripped for
        counting *and* for sending, so the counted request and the sent request
        can never disagree about what the provider will receive.
        """
        from .token_budget import strip_packing_hints
        return strip_packing_hints(request.messages)

    def output_limit(self) -> int:
        return self.profile.max_output_tokens

    def _provider_payload(self, request: ModelRequest) -> dict[str, Any]:
        """The exact body this profile's adapter sends. Pure: no I/O."""
        if self.profile.provider_protocol == "anthropic":
            return self._anthropic_payload(request)
        if self.profile.provider_protocol == "gemini":
            return self._gemini_payload(request)
        return self._openai_payload(request)

    def _openai_payload(self, request: ModelRequest) -> dict[str, Any]:
        """The exact body the OpenAI-compatible adapter sends. Pure: no I/O.

        Extracted so the packing layer can measure this adapter's real envelope
        instead of guessing it. A request packed to fill the whole budget was
        otherwise refused by the wire gate for the ~60 units of wrapper keys
        (``model``, ``stream``, ``stream_options``, ``temperature``,
        ``max_tokens``, ``thinking``) that sit outside ``messages``/``tools``.
        """
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": self._provider_messages(request),
            "tools": request.tools or [],
            "stream": True,
        }
        is_deepseek = self.profile.provider_name == "deepseek" or urlsplit(self.profile.base_url).hostname == "api.deepseek.com"
        if is_deepseek and request.thinking is not None:
            payload["thinking"] = {"type": "enabled" if request.thinking else "disabled"}
        if is_deepseek:
            payload["stream_options"] = {"include_usage": True}
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        return payload

    def _anthropic_payload(self, request: ModelRequest) -> dict[str, Any]:
        """The exact body the Anthropic adapter sends. Pure: no I/O.

        This adapter *rewrites* the messages: ``system`` messages are hoisted
        into one top-level string and tool definitions are reshaped. The wire
        gate therefore remains the authority for it.
        """
        provider_messages = self._provider_messages(request)
        system = "\n\n".join(str(message.get("content", "")) for message in provider_messages if message.get("role") == "system")
        messages = [message for message in provider_messages if message.get("role") != "system"]
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
        return payload

    def _gemini_payload(self, request: ModelRequest) -> dict[str, Any]:
        """The exact body the Gemini adapter sends. Pure: no I/O.

        Like the Anthropic adapter this *rebuilds* the messages (``contents``
        with ``parts``), so the wire gate stays the authority for it.
        """
        provider_messages = self._provider_messages(request)
        system = "\n\n".join(str(message.get("content", "")) for message in provider_messages if message.get("role") == "system")
        contents = [
            {"role": "model" if message.get("role") == "assistant" else "user", "parts": [{"text": str(message.get("content", ""))}]}
            for message in provider_messages if message.get("role") != "system"
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
        return payload

    async def complete(
        self,
        request: ModelRequest,
        cancel_event: asyncio.Event | None = None,
        on_text_delta: TextDeltaCallback | None = None,
        on_text_reset: TextResetCallback | None = None,
        on_attempt_started: AttemptStartedCallback | None = None,
        on_attempt_finished: AttemptFinishedCallback | None = None,
        on_output_started: OutputStartedCallback | None = None,
        context: Any | None = None,
    ) -> ModelResponse:
        from .token_budget import ContextOverflow, assert_request_fits

        try:
            assert_request_fits(request.messages, request.tools, self.profile)
        except ContextOverflow as exc:
            raise GatewayError(str(exc), "context_overflow") from exc
        effective_context = context or self._call_context.get()
        if self.control_store is not None:
            from .model_control import ModelCallContext, child_call_context

            effective_context = (
                child_call_context(
                    effective_context,
                    role=request.role,
                    purpose=request.purpose,
                )
                if effective_context is not None
                else ModelCallContext(role=request.role or "conversation", purpose=request.purpose or "complete")
            )
        handle = self.control_store.begin_invocation(self.profile, request, effective_context) if self.control_store is not None else None
        if cancel_event and cancel_event.is_set():
            if handle is not None:
                self.control_store.finish_invocation(handle, "cancelled")
            raise GatewayError("model request cancelled", "cancelled")
        from .config import resolve_credential
        api_key = resolve_credential(self.profile.api_key_env)
        if not api_key:
            if handle is not None:
                self.control_store.finish_invocation(handle, "failed")
            raise GatewayError("model API key missing", "configuration")
        attempt_count = 0
        network_retry_count = 0
        structure_retry_used = False
        context_retry_used = False
        while attempt_count < (1 if request.single_attempt else max(self.profile.max_attempts, 1)):
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

                response = await self._attempt(request, api_key, cancel_event, emit_delta, on_output_started)
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
                elif error.kind == "context_overflow":
                    # A capacity rejection is only worth another attempt when
                    # the request actually changes. This layer cannot shrink a
                    # payload - compaction belongs to the caller - so resending
                    # the identical body would burn a retry and mask the real
                    # signal. Record that the admitted capacity evidence no
                    # longer holds and fail closed instead of retrying blindly.
                    if not context_retry_used and handle is not None:
                        context_retry_used = True
                        self.control_store.record_event(handle, "model.context.capacity_evidence_invalidated", {
                            "model_invocation_id": handle.invocation_id,
                            "after_attempt": attempt_count,
                            "error_kind": error.kind,
                            "input_limit": self.input_limit(),
                        })
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "failed")
                    raise GatewayError(str(error), error.kind, attempt_count) from error
                elif error.kind not in {"rate_limit", "server", "timeout", "provider_unavailable"}:
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "failed")
                    raise GatewayError(str(error), error.kind, attempt_count) from error
                else:
                    network_retry_count += 1
                if request.single_attempt or attempt_count >= self.profile.max_attempts:
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "failed")
                    raise GatewayError("retry budget exhausted", error.kind, attempt_count) from error
                if error.kind in {"rate_limit", "server", "timeout", "provider_unavailable"} and network_retry_count > self.profile.network_retries:
                    if handle is not None:
                        self.control_store.finish_invocation(handle, "failed")
                    raise GatewayError("retry budget exhausted", error.kind, attempt_count) from error
                if handle is not None:
                    self.control_store.record_event(handle, "model.attempt.retry_scheduled", {
                        "model_invocation_id": handle.invocation_id, "after_attempt": attempt_count,
                        "next_attempt": attempt_count + 1, "error_kind": error.kind,
                    })
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
        on_output_started: OutputStartedCallback | None = None,
    ) -> ModelResponse:
        if self.profile.provider_protocol == "anthropic":
            return await self._attempt_anthropic(request, api_key, cancel_event, on_text_delta, on_output_started)
        if self.profile.provider_protocol == "gemini":
            return await self._attempt_gemini(request, api_key, cancel_event, on_text_delta, on_output_started)
        if self.profile.provider_protocol != "openai_compatible":
            raise GatewayError("unsupported model provider protocol", "configuration")
        return await self._attempt_openai(request, api_key, cancel_event, on_text_delta, on_output_started)

    async def _attempt_openai(
        self,
        request: ModelRequest,
        api_key: str,
        cancel_event: asyncio.Event | None,
        on_text_delta: TextDeltaCallback | None,
        on_output_started: OutputStartedCallback | None = None,
    ) -> ModelResponse:
        started = time.perf_counter()
        content: list[str] = []
        tool_call_fragments: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage = UsageBuckets()
        first_token_at: float | None = None
        payload = self._openai_payload(request)
        from .token_budget import assert_provider_payload_fits
        assert_provider_payload_fits(payload, self.profile)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        client = self.http_client or httpx.AsyncClient(
            transport=self.transport,
            timeout=self.profile.timeout_seconds,
        )
        try:
            async with client.stream(
                    "POST",
                    f"{self.profile.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
            ) as response:
                await _raise_for_status(response)
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
                            if on_output_started is not None:
                                on_output_started("tool_call")
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
        finally:
            if self.http_client is None:
                await client.aclose()
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
        on_output_started: OutputStartedCallback | None = None,
    ) -> ModelResponse:
        started = time.perf_counter()
        first_token_at: float | None = None
        content: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage = UsageBuckets()
        payload = self._anthropic_payload(request)
        from .token_budget import assert_provider_payload_fits
        assert_provider_payload_fits(payload, self.profile)
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
                                if on_output_started is not None:
                                    on_output_started("tool_call")
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
        on_output_started: OutputStartedCallback | None = None,
    ) -> ModelResponse:
        started = time.perf_counter()
        first_token_at: float | None = None
        content: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        finish_reason: str | None = None
        usage = UsageBuckets()
        payload = self._gemini_payload(request)
        from .token_budget import assert_provider_payload_fits
        assert_provider_payload_fits(payload, self.profile)
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
                                if on_output_started is not None:
                                    on_output_started("tool_call")
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


def provider_payload(profile: ModelProfile, request: ModelRequest) -> dict[str, Any]:
    """The exact body the adapter for ``profile`` would send. Pure: no I/O.

    The packing layer uses this to measure the adapter's real envelope. Guessing
    it - or ignoring it, as the first implementation did - lets the selector
    produce a request that fills the budget on ``messages``/``tools`` alone and
    is then refused by the wire gate for the wrapper keys.
    """
    return ModelGateway(profile)._provider_payload(request)


def normalize_usage(payload: dict[str, Any]) -> UsageBuckets:
    input_tokens = _int(payload.get("input_tokens", payload.get("prompt_tokens")))
    deepseek_usage = "prompt_cache_hit_tokens" in payload or "prompt_cache_miss_tokens" in payload
    explicit_uncached = _int(payload.get("uncached_input_tokens", payload.get("prompt_cache_miss_tokens")))
    cache_read = _int(payload.get("cache_read_tokens", payload.get("cached_tokens", payload.get("prompt_cache_hit_tokens"))))
    cache_write = _int(payload.get("cache_write_tokens", 0 if deepseek_usage else None))
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
        reasoning_tokens=_int(payload.get("reasoning_tokens", (payload.get("completion_tokens_details") or {}).get("reasoning_tokens", 0 if deepseek_usage else None))),
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
    if response.status_code == 402:
        raise GatewayError("model payment required", "payment")
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
