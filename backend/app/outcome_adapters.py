"""Boundary adapters for existing exceptions and loop domain return values."""
from __future__ import annotations

import asyncio

from .execution_outcome import Effect, ExecutionError, ExecutionOutcome, Phase, Scope, Status
from .execution_context import HarnessContextError
from .model_gateway import GatewayError
from .tool_contracts import ToolArgumentError, ToolReconciliationRequired, ToolRejected, ToolResult


_MODEL_KINDS = frozenset({"timeout", "provider_unavailable", "rate_limit", "server",
    "authentication", "configuration", "payment", "budget", "identity", "context_invalidated",
    "context_overflow", "structure", "request", "asset_revoked", "unknown"})
_TRANSIENT = frozenset({"timeout", "provider_unavailable", "rate_limit", "server"})
_MESSAGES = {
    "budget": "本次请求被预算限制阻止。", "payment": "模型服务余额不足或需要付款。",
    "authentication": "模型服务认证失败。", "configuration": "模型服务配置不可用。",
    "context_invalidated": "本次调用的来源已失效，请重新发起。",
    "identity": "执行身份无效。", "timeout": "模型请求超时。",
}


def error_from_exception(exc: BaseException, *, phase: Phase = Phase.LOOP) -> ExecutionError:
    if isinstance(exc, ToolReconciliationRequired):
        return ExecutionError("TOOL_RECONCILIATION_REQUIRED", Phase.TOOL, False, "工具执行结果尚未确认，需要核对后继续。")
    if isinstance(exc, ToolArgumentError):
        return ExecutionError("TOOL_INVALID_ARGUMENT", Phase.TOOL, False, "工具参数无效，请修正后重试。")
    if isinstance(exc, ToolRejected):
        return ExecutionError("TOOL_AUTHORIZATION_DENIED", Phase.AUTHORIZATION, False, "工具调用未获授权。")
    if isinstance(exc, HarnessContextError):
        return ExecutionError("IDENTITY_INVALID", Phase.AUTHORIZATION, False, "执行身份无效。")
    if isinstance(exc, GatewayError):
        kind = exc.kind if exc.kind in _MODEL_KINDS else "unknown"
        return ExecutionError("MODEL_" + kind.upper(), Phase.MODEL, kind in _TRANSIENT,
                              _MESSAGES.get(kind, "模型请求未完成，请检查服务状态。"), attempts=exc.attempts)
    return ExecutionError("INTERNAL_ERROR", phase, False, "执行未完成，请稍后重试或检查运行记录。")


def exception_outcome(exc: BaseException, *, scope: Scope = Scope.LOOP,
                      reconciliation_ref: str | None = None) -> ExecutionOutcome:
    if isinstance(exc, asyncio.CancelledError) or isinstance(exc, GatewayError) and exc.kind == "cancelled":
        return ExecutionOutcome(scope, Status.CANCELLED, reason="CANCELLED",
            effect=Effect.UNKNOWN if reconciliation_ref else Effect.NOT_APPLICABLE,
            reconciliation_ref=reconciliation_ref)
    error = error_from_exception(exc)
    if isinstance(exc, ToolReconciliationRequired):
        return ExecutionOutcome(scope, Status.RECONCILIATION_REQUIRED, error=error,
                                effect=Effect.UNKNOWN, reconciliation_ref=exc.operation_ref or reconciliation_ref)
    return ExecutionOutcome(scope, Status.FAILED, error=error,
        effect=Effect.NOT_STARTED if isinstance(exc, ToolRejected) else Effect.NOT_APPLICABLE)


def tool_result_outcome(result: ToolResult, *, write: bool, operation_ref: str) -> ExecutionOutcome:
    if result.ok:
        return ExecutionOutcome(Scope.TOOL, Status.COMPLETED, artifact_ref=result.artifact_ref,
                                effect=Effect.APPLIED if write else Effect.NOT_APPLICABLE)
    if write and result.effect != Effect.NOT_STARTED:
        return exception_outcome(ToolReconciliationRequired(), scope=Scope.TOOL, reconciliation_ref=operation_ref)
    if result.error in {"INVALID_ARGUMENT", "TOOL_NOT_ALLOWED", "TOOL_AUTHORIZATION_DENIED"}:
        rejected = ToolArgumentError() if result.error == "INVALID_ARGUMENT" else ToolRejected()
        return exception_outcome(rejected, scope=Scope.TOOL)
    return ExecutionOutcome(Scope.TOOL, Status.FAILED,
        error=ExecutionError("TOOL_EXECUTION_FAILED", Phase.TOOL, False, "工具未能完成请求。"),
        effect=Effect.NOT_STARTED if write else Effect.NOT_APPLICABLE)


def loop_outcome(value, *, operation_ref: str) -> ExecutionOutcome:
    # Local import avoids a cycle: AgentLoop exposes this adapter as a boundary.
    from .agent_loop import Final, Terminal, Suspended, Handoff, Exhausted, Failed, Cancelled
    if isinstance(value, Final):
        ref = value.artifact.get("version_id") if isinstance(value.artifact, dict) else None
        return ExecutionOutcome(Scope.LOOP, Status.COMPLETED, artifact_ref=ref)
    if isinstance(value, Terminal):
        if value.name == "await_outcome":
            return ExecutionOutcome(Scope.LOOP, Status.AWAITING_EXTERNAL, continuation_ref=operation_ref)
        if value.name == "report_blocked":
            return ExecutionOutcome(Scope.LOOP, Status.BLOCKED, reason="MODEL_REQUESTED_BLOCK")
        if value.name == "finish_step":
            return ExecutionOutcome(Scope.LOOP, Status.COMPLETED)
        return ExecutionOutcome(Scope.LOOP, Status.FAILED,
            error=ExecutionError("INVALID_MODEL_ACTION", Phase.LOOP, False, "模型返回了不支持的结束操作。"))
    if isinstance(value, Suspended):
        pending = value.continuation
        ref = pending if isinstance(pending, str) else (
            getattr(pending, "approval_id", None) or getattr(pending, "call_id", None))
        status = Status.AWAITING_INPUT if value.kind == "ask" else Status.AWAITING_APPROVAL
        return ExecutionOutcome(Scope.LOOP, status, continuation_ref=ref)
    if isinstance(value, Handoff):
        return ExecutionOutcome(Scope.LOOP, Status.HANDOFF, target_ref=value.ref)
    if isinstance(value, Exhausted):
        return ExecutionOutcome(Scope.LOOP, Status.EXHAUSTED, reason=value.reason)
    if isinstance(value, Cancelled):
        return ExecutionOutcome(Scope.LOOP, Status.CANCELLED, reason=value.reason)
    if isinstance(value, Failed):
        return exception_outcome(value.error, reconciliation_ref=(
            operation_ref if isinstance(value.error, ToolReconciliationRequired) else None))
    raise TypeError("unsupported loop outcome")
