import pytest


def test_write_approval_is_bound_to_tool_call_and_normalized_params(tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalRequired, ApprovalService

    service = ApprovalService(Database(tmp_path / "agent.db"))
    approval = service.request("run-1", "call-1", "write_note", {"path": "a.md", "content": "x"})

    with pytest.raises(ApprovalRequired):
        service.require_granted("run-1", "call-1", {"path": "a.md", "content": "x"})
    with pytest.raises(ApprovalRequired):
        service.grant(approval.id, "run-1", "call-1", {"path": "a.md", "content": "changed"})

    service.grant(approval.id, "run-1", "call-1", {"content": "x", "path": "a.md"})
    service.require_granted("run-1", "call-1", {"path": "a.md", "content": "x"})

    with pytest.raises(ApprovalRequired):
        service.require_granted("other-run", "call-1", {"path": "a.md", "content": "x"})


def test_rejected_and_expired_approval_cannot_execute(tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalRequired, ApprovalService

    service = ApprovalService(Database(tmp_path / "agent.db"))
    rejected = service.request("run-1", "call-1", "write_note", {"path": "a.md", "content": "x"})
    service.reject(rejected.id, "run-1", "call-1", {"path": "a.md", "content": "x"})
    with pytest.raises(ApprovalRequired):
        service.require_granted("run-1", "call-1", {"path": "a.md", "content": "x"})

    expired = service.request(
        "run-1",
        "call-2",
        "write_note",
        {"path": "b.md", "content": "x"},
        expires_at="2000-01-01T00:00:00+00:00",
    )
    with pytest.raises(ApprovalRequired):
        service.grant(expired.id, "run-1", "call-2", {"path": "b.md", "content": "x"})
import pytest

from test_runtime import make_runtime


@pytest.mark.asyncio
async def test_rejected_write_is_not_re_requested_or_executed(tmp_path) -> None:
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall

    call = ToolCall("reject-once", "write_note", {"path": "rejected.md", "content": "no"})
    runtime = make_runtime(tmp_path, MockModelGateway(
        plan_steps=[{"id": "step-1", "title": "Write"}],
        decisions=[ModelDecision.tool(call)],
    ))
    run = await runtime.create_goal("Reject", "Do not write")
    await runtime.handle_message(run.id, "Write with approval")
    await runtime.approve_plan(run.id, 1)
    approval = runtime.pending_approvals(run.id)[0]

    completed = await runtime.reject_approval(approval.id)

    assert completed.state.value == "COMPLETED"
    assert not (tmp_path / "workspace" / "rejected.md").exists()
    assert len(runtime.pending_approvals(run.id)) == 0


def test_re_request_after_rejection_reuses_binding_without_duplicate_row(tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalService

    service = ApprovalService(Database(tmp_path / "agent.db"))
    params = {"path": "note.md", "content": "one"}
    first = service.request("run-1", "call-1", "write_note", params)
    service.reject(first.id, "run-1", "call-1", params)

    second = service.request("run-1", "call-1", "write_note", params)

    assert second.id == first.id
    with service.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
