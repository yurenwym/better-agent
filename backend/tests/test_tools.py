import pytest


def test_tool_descriptions_use_openai_function_wire_format(tmp_path) -> None:
    from app.tools import create_default_registry

    registry = create_default_registry(tmp_path / "workspace")

    descriptions = registry.describe()

    assert descriptions
    assert {tool["type"] for tool in descriptions} == {"function"}
    first = descriptions[0]
    assert set(first["function"]) == {"name", "description", "parameters"}
    assert first["function"]["parameters"]["type"] == "object"


def test_builtin_tools_are_safe_and_return_standard_results(tmp_path) -> None:
    from app.tools import ToolCall, ToolRejected, create_default_registry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.md").write_text("hello", encoding="utf-8")
    registry = create_default_registry(workspace)

    now = registry.execute(ToolCall("call-time", "local_time", {}), run_id="run-1", skill_tools=None)
    calculated = registry.execute(
        ToolCall("call-calc", "calculator", {"expression": "2 * (3 + 4)"}),
        run_id="run-1",
        skill_tools=None,
    )
    note = registry.execute(
        ToolCall("call-read", "read_note", {"path": "note.md"}),
        run_id="run-1",
        skill_tools={"read_note"},
    )

    assert now.ok and now.data["timezone"]
    assert calculated.ok and calculated.data["value"] == 14
    assert note.ok and note.data["content"] == "hello"

    with pytest.raises(ToolRejected):
        registry.execute(ToolCall("unknown", "shell", {}), run_id="run-1", skill_tools=None)
    with pytest.raises(ToolRejected):
        registry.execute(
            ToolCall("bad-calc", "calculator", {"expression": "__import__('os').getcwd()"}),
            run_id="run-1",
            skill_tools=None,
        )
    with pytest.raises(ToolRejected):
        registry.execute(
            ToolCall("escape", "read_note", {"path": "../outside.md"}),
            run_id="run-1",
            skill_tools={"read_note"},
        )


def test_read_and_write_tools_require_skill_intersection_and_write_approval(tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalRequired, ApprovalService
    from app.tools import ToolCall, ToolRejected, create_default_registry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "agent.db", workspace=workspace)
    approvals = ApprovalService(db)
    registry = create_default_registry(workspace, db=db, approval_service=approvals)
    call = ToolCall("write-1", "write_note", {"path": "new.md", "content": "first"})

    with pytest.raises(ToolRejected):
        registry.execute(call, run_id="run-1", skill_tools={"read_note"})
    with pytest.raises(ApprovalRequired):
        registry.execute(call, run_id="run-1", skill_tools={"write_note"})

    approval = approvals.request("run-1", call.id, call.name, call.params)
    approvals.grant(approval.id, "run-1", call.id, call.params)
    result = registry.execute(call, run_id="run-1", skill_tools={"write_note"})

    assert result.ok
    assert (workspace / "new.md").read_text(encoding="utf-8") == "first"
    repeated = registry.execute(call, run_id="run-1", skill_tools={"write_note"})
    assert repeated.data == result.data


def test_tool_registry_returns_standard_timeout_result(tmp_path) -> None:
    import time

    from app.tools import ToolCall, ToolRegistry, ToolResult, ToolRisk, ToolSpec

    registry = ToolRegistry(tmp_path / "workspace")
    registry.register(ToolSpec(
        name="slow",
        description="slow",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.PURE,
        handler=lambda params: (time.sleep(0.05), ToolResult(True, "done"))[1],
        timeout_seconds=0.001,
    ))

    result = registry.execute(ToolCall("slow-1", "slow", {}), run_id="run-1", skill_tools=None)

    assert result.ok is False
    assert result.error == "timeout"
