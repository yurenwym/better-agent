"""Capability definitions and trusted call/result types; no infrastructure dependencies."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable
from .execution_context import HarnessContextError, HarnessExecutionContext, check_context_alignment
from .execution_outcome import Effect


class ToolRisk(StrEnum):
    PURE = "PURE"
    READ = "READ"
    WRITE = "WRITE"


class ToolRejected(PermissionError):
    pass


class ToolArgumentError(ToolRejected):
    """The selected capability exists, but its arguments are invalid."""


class ToolReconciliationRequired(ToolRejected):
    def __init__(self, message: str = "tool execution requires reconciliation", *, operation_ref: str | None = None):
        super().__init__(message)
        self.operation_ref = operation_ref


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    artifact_ref: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    effect: Effect | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "artifact_ref": self.artifact_ref,
            "error": self.error,
            "meta": self.meta,
        }
        if self.effect is not None:
            result["effect"] = Effect(self.effect).value
        return result


Handler = Callable[[dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolExecutionContext:
    """Trusted server-side identity for one tool invocation.

    Built from the durable run/approval records by the Runtime, never from
    model-supplied parameters. Business handlers read ``owner_id`` from here so
    two owners running concurrently can never share an identity through a
    global or ambient variable.
    """

    owner_id: str
    run_id: str
    tool_call_id: str
    thread_id: str | None = None
    turn_id: str | None = None
    project_id: str | None = None
    # Budget/bundle identity of the originating run, so a tool-triggered model
    # compile can share the run's root budget instead of creating an unrelated
    # goal_operation budget.
    root_budget_id: str | None = None
    runtime_bundle_id: str | None = None
    authorization: dict[str, Any] | None = None
    # Span identity of the logical tool call.  An approval that waits for a
    # human keeps this span; only a genuinely new internal model call gets a
    # child span of it.
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    harness: HarnessExecutionContext | None = None

    def __post_init__(self) -> None:
        check_context_alignment(self)

    @classmethod
    def from_harness(
        cls,
        harness: HarnessExecutionContext,
        *,
        tool_call_id: str,
        authorization: dict[str, Any] | None = None,
    ) -> "ToolExecutionContext":
        """Convert a harness context into a tool call identity.

        A conversion, not a factory: no trace/span is minted and no task or
        budget is created.  A tool call without a run binding is refused rather
        than silently given one.
        """
        if not isinstance(harness, HarnessExecutionContext):
            raise HarnessContextError("harness must be a HarnessExecutionContext")
        if not harness.run_id:
            raise HarnessContextError("a tool call requires a run binding")
        return cls(
            owner_id=harness.owner_id,
            run_id=harness.run_id,
            tool_call_id=tool_call_id,
            thread_id=harness.thread_id,
            turn_id=harness.turn_id,
            project_id=harness.project_id,
            root_budget_id=harness.root_budget_id,
            runtime_bundle_id=harness.runtime_bundle_id,
            authorization=authorization,
            trace_id=harness.trace_id,
            span_id=harness.span_id,
            parent_span_id=harness.parent_span_id,
            task_id=harness.task_id,
            parent_task_id=harness.parent_task_id,
            root_task_id=harness.root_task_id,
            harness=harness,
        )

    def public_view(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "root_budget_id": self.root_budget_id,
            "runtime_bundle_id": self.runtime_bundle_id,
        }


ContextHandler = Callable[[dict[str, Any], ToolExecutionContext], ToolResult]
Validator = Callable[[dict[str, Any]], None]

# Parameters a model must never supply: identity and idempotency are injected by
# the harness. A tool whose params carry any of these is rejected before
# approval or execution.
IDENTITY_PARAMETER_NAMES = frozenset({
    "owner_id", "run_id", "tool_call_id", "thread_id", "project_id",
    "idempotency_key", "operation_key",
})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    risk: ToolRisk
    handler: Handler
    timeout_seconds: float = 30
    path_fields: tuple[str, ...] = ()
    # A context-aware handler replaces ``handler`` when present; it receives the
    # trusted execution context. The Runtime awaits it through
    # ``ToolRegistry.execute_async``.
    context_handler: ContextHandler | None = None
    # Pre-approval/pre-execution argument validation. Must raise ToolRejected
    # for invalid input so the model never sees an unvalidated call reach an
    # approval record.
    validator: Validator | None = None
    # Context tools reject harness-owned parameter names outright.
    reject_identity_params: bool = False
    # Read-only reconciliation: return only a proven committed business result.
    recover_result: ContextHandler | None = None
    # Provenance. Native tools leave these at their defaults; an MCP tool must
    # be locatable by source, stable server identity, remote name and the
    # digest of the definition the model was actually shown.
    source: str = "native"
    server_id: str | None = None
    remote_name: str | None = None
    definition_digest: str | None = None
    # Bump when native handler semantics change without a schema change.
    version: str = "1"

    def approval_digest(self) -> str:
        definition = {"name": self.name, "version": self.version, "schema": self.schema,
                      "risk": self.risk.value, "path_fields": self.path_fields,
                      "reject_identity_params": self.reject_identity_params,
                      "source": self.source, "definition_digest": self.definition_digest}
        return hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode()).hexdigest()


