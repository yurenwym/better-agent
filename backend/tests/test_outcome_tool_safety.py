import asyncio
import pytest

from app.db import Database
from app.domain import ApprovalService
from app.execution_context import create_root_context
from app.tools import (ToolCall, ToolExecutionContext, ToolReconciliationRequired, ToolRejected,
                       ToolRegistry, ToolResult, ToolRisk, ToolSpec)
from app.execution_outcome import Effect, Phase, Scope
from app.outcome_adapters import exception_outcome


@pytest.mark.parametrize("params,allowed,code,phase", [
    ({}, None, "TOOL_INVALID_ARGUMENT", Phase.TOOL),
    ({"value": 42}, None, "TOOL_INVALID_ARGUMENT", Phase.TOOL),
    ({"value": "valid"}, set(), "TOOL_AUTHORIZATION_DENIED", Phase.AUTHORIZATION),
])
def test_rejection_outcomes_distinguish_arguments_from_permissions(tmp_path, params, allowed, code, phase):
    effects = []
    registry = ToolRegistry(tmp_path)
    registry.register(ToolSpec("read", "read", {
        "type": "object", "required": ["value"], "properties": {"value": {"type": "string"}},
    }, ToolRisk.READ, lambda params: effects.append(params)))
    with pytest.raises(ToolRejected) as rejected:
        registry.authorize(ToolCall("call", "read", params), run_id="run", skill_tools=allowed)
    outcome = exception_outcome(rejected.value, scope=Scope.TOOL)
    assert outcome.error.code == code and outcome.error.phase == phase
    assert outcome.effect == Effect.NOT_STARTED
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("params,code", [({}, "INVALID_ARGUMENT"), ({"owner_id": "foreign"}, "TOOL_NOT_ALLOWED")])
async def test_chat_preflight_classifies_rejection_before_recording_or_execution(tmp_path, params, code):
    from app.chat_tools import ChatToolRunner
    registry = ToolRegistry(tmp_path)
    registry.register(ToolSpec("read", "read", {
        "type": "object", "required": ["value"], "properties": {"value": {"type": "string"}},
    }, ToolRisk.READ, lambda _: pytest.fail("rejected tool executed"), reject_identity_params=True))
    runner = ChatToolRunner(registry=registry, approvals=None, store=None, turn_id="turn", thread_id="thread",
                            owner_id="owner", project_id=None, skill_names=(), capability_names=frozenset({"read"}))
    result = (await runner.execute(tool_name="read", params=params)).result
    assert result.error == code
    assert result.effect == Effect.NOT_STARTED


def test_mcp_invalid_arguments_preserve_compatibility_and_error_class():
    from app.mcp_tools import McpToolRejected, validate_arguments
    with pytest.raises(McpToolRejected) as rejected:
        validate_arguments({"type": "object", "properties": {"n": {"type": "integer"}}}, {"n": "private-value"})
    outcome = exception_outcome(rejected.value, scope=Scope.TOOL)
    assert outcome.error.code == "TOOL_INVALID_ARGUMENT"
    assert "private-value" not in outcome.error.public_message


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "cancel", "false", "invalid"])
async def test_unconfirmed_write_is_durable_and_never_resent(tmp_path, failure):
    db = Database(tmp_path / "agent.db")
    approvals = ApprovalService(db)
    effects = []

    async def handler(params, context):
        effects.append("written")
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "exception":
            raise RuntimeError("response lost")
        return ToolResult(False, "unconfirmed") if failure == "false" else None

    spec = ToolSpec("write", "write", {"type": "object", "properties": {}}, ToolRisk.WRITE,
                    lambda _: None, context_handler=handler)
    call = ToolCall("one", "write", {})
    approval = approvals.request("run", call.id, call.name, call.params)
    approvals.grant(approval.id, "run", call.id, call.params)
    context = ToolExecutionContext.from_harness(create_root_context(owner_id="owner", run_id="run"), tool_call_id=call.id)
    registry = ToolRegistry(tmp_path, db=db, approval_service=approvals)
    registry.register(spec)
    expected = asyncio.CancelledError if failure == "cancel" else ToolReconciliationRequired
    with pytest.raises(expected):
        await registry.execute_async(call, context=context, skill_tools=None)
    with db.connection() as conn:
        assert conn.execute("SELECT status FROM tool_execution_claims WHERE tool_call_id='one'").fetchone()[0] == "RECONCILIATION_REQUIRED"
    # A reconstructed registry has no in-memory knowledge of the first effect.
    restored = ToolRegistry(tmp_path, db=db, approval_service=approvals)
    restored.register(spec)
    with pytest.raises(ToolReconciliationRequired) as rejected:
        await restored.execute_async(call, context=context, skill_tools=None)
    assert rejected.value.operation_ref == call.id
    assert effects == ["written"]
