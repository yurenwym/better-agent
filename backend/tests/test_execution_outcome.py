import json

import pytest

from app.execution_outcome import Effect, ExecutionError, ExecutionOutcome, Phase, Scope, Status
from app.outcome_adapters import exception_outcome, loop_outcome, tool_result_outcome


@pytest.mark.parametrize("status,fields", [
    (Status.COMPLETED, {"artifact_ref": "version-1"}),
    (Status.AWAITING_INPUT, {"continuation_ref": "ask-1"}),
    (Status.AWAITING_APPROVAL, {"continuation_ref": "approval-1"}),
    (Status.AWAITING_EXTERNAL, {"continuation_ref": "run-1"}),
    (Status.BLOCKED, {"reason": "MODEL_REQUESTED_BLOCK"}),
    (Status.HANDOFF, {"target_ref": "job-1"}),
    (Status.CANCELLED, {"reason": "CANCELLED"}),
    (Status.EXHAUSTED, {"reason": "STEP_WALL_TIME_EXHAUSTED"}),
    (Status.FAILED, {"error": ExecutionError("INTERNAL_ERROR", Phase.LOOP, False, "失败")}),
    (Status.RECONCILIATION_REQUIRED, {"effect": Effect.UNKNOWN, "reconciliation_ref": "call-1",
        "error": ExecutionError("TOOL_RECONCILIATION_REQUIRED", Phase.TOOL, False, "核对")}),
])
def test_roundtrip(status, fields):
    value = ExecutionOutcome(Scope.LOOP, status, **fields)
    assert ExecutionOutcome.from_dict(json.loads(json.dumps(value.to_dict()))) == value


@pytest.mark.parametrize("fields", [
    {"status": "awaiting_input"}, {"status": "handoff"}, {"status": "failed"},
    {"status": "completed", "continuation_ref": "unexpected"},
    {"status": "completed", "effect": "unknown", "reconciliation_ref": "call"},
    {"status": "cancelled"}, {"status": "completed", "error": RuntimeError("secret")},
])
def test_illegal_states_rejected(fields):
    with pytest.raises(ValueError):
        ExecutionOutcome(Scope.LOOP, **fields)


def test_unknown_version_rejected():
    value = ExecutionOutcome(Scope.MODEL, Status.COMPLETED).to_dict()
    value["schema_version"] = True
    with pytest.raises(ValueError):
        ExecutionOutcome.from_dict(value)


def test_errors_are_safe_and_unknown_is_not_retryable():
    from app.model_gateway import GatewayError
    for exc in [RuntimeError("sk-secret"), GatewayError("sk-secret", "new-provider-kind")]:
        outcome = exception_outcome(exc)
        assert not outcome.error.retryable
        assert "sk-secret" not in json.dumps(outcome.to_dict())
    assert exception_outcome(RuntimeError()).error.code == "INTERNAL_ERROR"
    timeout = exception_outcome(GatewayError("secret", "timeout", 3))
    assert timeout.error.attempts == 3 and timeout.error.retryable


def test_reconciliation_precedes_rejection_and_write_failure_is_unknown():
    from app.tools import ToolReconciliationRequired, ToolRejected, ToolResult
    result = exception_outcome(ToolReconciliationRequired(), reconciliation_ref="call")
    assert result.status == Status.RECONCILIATION_REQUIRED
    assert result.effect == Effect.UNKNOWN and not result.error.retryable
    assert exception_outcome(ToolRejected()).effect == Effect.NOT_STARTED
    assert tool_result_outcome(ToolResult(False, "failed"), write=True, operation_ref="call").status == Status.RECONCILIATION_REQUIRED


def test_terminal_semantics_are_not_tool_success():
    from app.agent_loop import Terminal
    assert loop_outcome(Terminal("await_outcome", {}), operation_ref="run").status == Status.AWAITING_EXTERNAL
    assert loop_outcome(Terminal("report_blocked", {}), operation_ref="run").status == Status.BLOCKED
    assert loop_outcome(Terminal("finish_step", {}), operation_ref="run").scope == Scope.LOOP


@pytest.mark.parametrize("code,expected", [
    ("INVALID_ARGUMENT", "TOOL_INVALID_ARGUMENT"),
    ("TOOL_NOT_ALLOWED", "TOOL_AUTHORIZATION_DENIED"),
])
def test_known_tool_result_errors_do_not_override_uncertain_writes(code, expected):
    from app.tools import ToolResult
    rejected = tool_result_outcome(ToolResult(False, "private-detail", error=code, effect=Effect.NOT_STARTED),
                                   write=True, operation_ref="call")
    assert rejected.error.code == expected
    assert rejected.effect == Effect.NOT_STARTED
    assert "private-detail" not in json.dumps(rejected.to_dict())
    uncertain = tool_result_outcome(ToolResult(False, "private-detail", error=code),
                                    write=True, operation_ref="call")
    assert uncertain.status == Status.RECONCILIATION_REQUIRED
    assert uncertain.reconciliation_ref == "call"
