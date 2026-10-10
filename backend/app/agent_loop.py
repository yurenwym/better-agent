"""Business-independent contracts for bounded model/tool execution."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Protocol

from .execution_context import HarnessExecutionContext, create_child_context
from .model_control import ModelCallContext
from .model_gateway import ModelResponse
from .tool_contracts import ToolCall
from .execution_outcome import ExecutionOutcome


@dataclass(frozen=True)
class LoopGuards:
    max_iterations: int = 8
    max_wall_time_seconds: float = 300
    max_identical_actions: int = 2
    max_consecutive_tool_errors: int = 2
    max_repeated_failure: int = 2

    def __post_init__(self) -> None:
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("loop guard limits must be positive")


@dataclass(frozen=True)
class LoopInput:
    messages: list[dict[str, Any]]
    harness: HarnessExecutionContext | None
    cancel_event: asyncio.Event | None = None
    guard_state: dict | None = None


@dataclass(frozen=True)
class CapabilitySet:
    schemas: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AgentProfile:
    name: str
    role: str
    purpose_prefix: str
    system_prompt: Callable[[LoopInput], list[dict[str, Any]]]
    capabilities: Callable[[LoopInput], CapabilitySet]
    terminal: frozenset[str] = frozenset()
    handoff: frozenset[str] = frozenset()
    bind_text: frozenset[str] = frozenset()
    allow_text_final: bool = True
    guards: LoopGuards = field(default_factory=LoopGuards)
    stream_text: bool = True
    version: str = "1"


@dataclass(frozen=True)
class LoopCallbacks:
    on_text_delta: Callable[[str], None] | None = None
    on_text_reset: Callable[[], None] | None = None


class CapabilityKind(StrEnum):
    RESULT = "result"
    PENDING = "pending"
    HANDOFF = "handoff"
    TERMINAL = "terminal"
    BIND_TEXT = "bind_text"


@dataclass(frozen=True)
class CapabilityOutcome:
    kind: CapabilityKind = CapabilityKind.RESULT
    result: Any = None
    ok: bool = True
    continuation: Any = None
    ref: str | None = None
    category: str | None = None
    artifact: Any = None
    call_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", CapabilityKind(self.kind))


@dataclass(frozen=True)
class Final:
    text: str
    artifact: Any = None


@dataclass(frozen=True)
class Terminal:
    name: str
    params: dict[str, Any]
    result: Any = None


@dataclass(frozen=True)
class Suspended:
    kind: str
    continuation: Any


@dataclass(frozen=True)
class Handoff:
    kind: str
    ref: str


@dataclass(frozen=True)
class Exhausted:
    reason: str


@dataclass(frozen=True)
class Failed:
    error: Any


@dataclass(frozen=True)
class Cancelled:
    reason: str = "CANCELLED"


LoopOutcome = Final | Terminal | Suspended | Handoff | Exhausted | Failed | Cancelled


@dataclass(frozen=True)
class LoopExecution:
    """Public semantics plus the in-process domain value used by adapters.

    Persist only outcome.to_dict(); value may contain private text or an exception.
    """
    outcome: ExecutionOutcome
    value: LoopOutcome


class LoopModel(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        context: ModelCallContext | None, callbacks: LoopCallbacks,
    ) -> ModelResponse: ...


class CapabilityExecutor(Protocol):
    async def execute(self, call: ToolCall, *, bound_text: str | None = None) -> CapabilityOutcome: ...


class AgentLoop:
    def __init__(self, model: LoopModel, executor: CapabilityExecutor) -> None:
        self.model = model
        self.executor = executor

    async def run_execution(self, profile: AgentProfile, inputs: LoopInput,
                            callbacks: LoopCallbacks = LoopCallbacks(), *,
                            operation_ref: str) -> LoopExecution:
        from .outcome_adapters import loop_outcome
        value = await self.run(profile, inputs, callbacks)
        return LoopExecution(loop_outcome(value, operation_ref=operation_ref), value)

    async def run(self, profile: AgentProfile, inputs: LoopInput,
                  callbacks: LoopCallbacks = LoopCallbacks()) -> LoopOutcome:
        """Bound the entire execution, cancelling in-flight work on stop/timeout."""
        if inputs.cancel_event is not None and inputs.cancel_event.is_set():
            return Cancelled()
        work = asyncio.create_task(self._run(profile, inputs, callbacks))
        cancelled = asyncio.create_task(inputs.cancel_event.wait()) if inputs.cancel_event is not None else None
        try:
            done, _ = await asyncio.wait(
                [work, *([cancelled] if cancelled is not None else [])],
                timeout=profile.guards.max_wall_time_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled is not None and cancelled in done:
                return Cancelled()
            if work in done:
                return work.result()
            return Exhausted("STEP_WALL_TIME_EXHAUSTED")
        except asyncio.CancelledError:
            return Cancelled()
        except Exception as exc:
            return Failed(exc)
        finally:
            for task in (work, cancelled):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (work, cancelled) if task is not None], return_exceptions=True)

    async def _run(self, profile: AgentProfile, inputs: LoopInput, callbacks: LoopCallbacks) -> LoopOutcome:
        messages = [*profile.system_prompt(inputs), *inputs.messages]
        tools = list(profile.capabilities(inputs).schemas)
        guard_state = inputs.guard_state if inputs.guard_state is not None else {}
        actions: Counter[str] = Counter(guard_state.get("actions", {}))
        failures: Counter[str] = Counter(guard_state.get("failures", {}))
        consecutive_errors = guard_state.get("consecutive_errors", 0)
        started = time.monotonic()

        async def complete(purpose: str, schemas: list[dict]) -> ModelResponse:
            context = ModelCallContext.from_harness(
                create_child_context(inputs.harness), role=profile.role,
                purpose=purpose, invocation_id=uuid.uuid4().hex,
            ) if inputs.harness is not None else ModelCallContext(role=profile.role, purpose=purpose)
            return await self.model.complete(messages, schemas, context, callbacks if profile.stream_text else LoopCallbacks())

        for iteration in range(profile.guards.max_iterations):
            if inputs.cancel_event is not None and inputs.cancel_event.is_set():
                return Cancelled()
            if time.monotonic() - started >= profile.guards.max_wall_time_seconds:
                return Exhausted("STEP_WALL_TIME_EXHAUSTED")
            purpose = profile.purpose_prefix if iteration == 0 else f"{profile.purpose_prefix}_tool_{iteration}"
            response = await complete(purpose, tools)
            if not response.tool_calls:
                if profile.allow_text_final:
                    return Final(response.message)
                messages.extend([{"role": "assistant", "content": response.message},
                                 {"role": "system", "content": "必须调用终止型能力来结束本步骤。"}])
                continue
            binding_only = all(call.get("function", {}).get("name") in profile.bind_text for call in response.tool_calls)
            if not binding_only and callbacks.on_text_reset is not None:
                callbacks.on_text_reset()
            bound_text = response.message if binding_only else None
            repeat_failure = False
            for raw in response.tool_calls:
                if inputs.cancel_event is not None and inputs.cancel_event.is_set():
                    return Cancelled()
                function = raw.get("function", {})
                name = function.get("name", "")
                call_id = raw.get("id") or uuid.uuid4().hex
                invalid = False
                try:
                    arguments = function.get("arguments", "{}")
                    params = json.loads(arguments) if isinstance(arguments, str) else arguments
                    if not isinstance(params, dict):
                        raise ValueError("object required")
                except (ValueError, TypeError):
                    params, invalid = {}, True
                signature = name + (":invalid" if invalid else ":" + json.dumps(params, sort_keys=True, ensure_ascii=False))
                actions[signature] += 1
                guard_state["actions"] = dict(actions)
                if actions[signature] > profile.guards.max_identical_actions:
                    return Exhausted("IDENTICAL_ACTION_BUDGET_EXHAUSTED")
                call = ToolCall(call_id, name, params)
                if invalid:
                    outcome = CapabilityOutcome(ok=False, result={"ok": False, "error": "INVALID_ARGUMENT"})
                elif name in profile.bind_text and not binding_only:
                    outcome = CapabilityOutcome(ok=False, result={"ok": False, "error": "NEED_BOUND_TEXT"})
                else:
                    outcome = await self.executor.execute(call, bound_text=bound_text)
                if outcome.kind == CapabilityKind.PENDING:
                    return Suspended(outcome.category or "approval", outcome.continuation)
                if outcome.kind == CapabilityKind.HANDOFF:
                    return Handoff(outcome.category or name, outcome.ref or "")
                if outcome.kind == CapabilityKind.TERMINAL:
                    return Terminal(name, params, outcome.result)
                if outcome.kind == CapabilityKind.BIND_TEXT and outcome.ok:
                    return Final(bound_text or "", outcome.artifact)
                call_id = outcome.call_id or call_id
                messages.extend([
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(params, ensure_ascii=False, sort_keys=True)}}]},
                    {"role": "tool", "tool_call_id": call_id, "content": outcome.result if isinstance(outcome.result, str) else json.dumps(outcome.result, ensure_ascii=False)},
                ])
                if outcome.ok:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    failures[signature] += 1
                    guard_state.update(failures=dict(failures), consecutive_errors=consecutive_errors)
                    if failures[signature] >= profile.guards.max_repeated_failure:
                        repeat_failure = True
                        break
                    if consecutive_errors >= profile.guards.max_consecutive_tool_errors:
                        return Exhausted("CONSECUTIVE_TOOL_ERRORS_EXHAUSTED")
                guard_state.update(failures=dict(failures), consecutive_errors=consecutive_errors)
            if repeat_failure:
                messages.append({"role": "system", "content": "同一工具调用已经重复失败。停止重试，不要再次调用工具；向用户说明失败原因，并给出可执行的下一步或需要补充的信息。"})
                response = await complete(f"{profile.purpose_prefix}_tool_stop_{iteration}", [])
                if response.tool_calls or not profile.allow_text_final:
                    return Exhausted("CONSECUTIVE_TOOL_ERRORS_EXHAUSTED")
                return Final(response.message)
        return Exhausted("REACT_ITERATION_BUDGET_EXHAUSTED")
