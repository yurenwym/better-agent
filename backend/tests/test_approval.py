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
