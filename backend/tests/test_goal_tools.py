"""T0/P1/P2/T1-T7: goal business tools through the existing runtime.

Business semantics live in the existing services; these tests pin the adapter
contract: trusted identity, strict parameters, approval gating, idempotent
replay, and the draft -> preview -> activate main chain.
"""

from __future__ import annotations

import asyncio

import pytest

from test_goal_program_compiler import fixture


def _runtime(tmp_path, monkeypatch=None, *, enabled: bool = True):
    if monkeypatch is not None:
        monkeypatch.setenv("GOAL_TOOLS_ENABLED", "1" if enabled else "0")
    from app.startup import build_runtime

    return build_runtime(tmp_path)


def _tool_runtime(tmp_path):
    """Runtime with the deterministic compiler and the goal tools registered.

    ``build_runtime`` wires a live gateway compiler when a profile is present,
    which cannot compile offline; these tests exercise the adapters and the
    runtime loop, not the model.
    """
    from app.goal_tools import register_goal_tools
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway())
    register_goal_tools(
        runtime.tools,
        goal_programs=runtime.goal_programs,
        plan_documents=runtime.plan_documents,
    )
    return runtime


def _context(runtime, thread_id, *, run="run-1", call="call-1", owner="local-user"):
    from app.tools import ToolExecutionContext

    return ToolExecutionContext(
        owner_id=owner, run_id=run, tool_call_id=call,
        thread_id=thread_id, project_id=None,
        root_budget_id=None, runtime_bundle_id=None,
    )


def _approved_activation_context(runtime, thread_id, params, *, call="call-activate"):
    from dataclasses import replace
    from app.goal_tools import activation_approval_binding
    context = _context(runtime, thread_id, run="run-main", call=call)
    binding = activation_approval_binding(runtime.goal_programs, runtime.plan_documents, params, context)
    approval = runtime.approvals.request(context.run_id, call, "activate_goal_plan", params, binding=binding)
    runtime.approvals.grant(approval.id, context.run_id, call, params, binding=binding)
    return replace(context, authorization=binding)


def _handler(runtime, name):
    return runtime.tools._tools[name].context_handler


async def _execute_with_approval(runtime, call, context):
    from app.domain import ApprovalRequired

    try:
        return await runtime.tools.execute_async(call, context=context, skill_tools=None)
    except ApprovalRequired:
        approval = runtime.approvals.request(context.run_id, call.id, call.name, call.params)
        runtime.approvals.grant(approval.id, context.run_id, call.id, call.params)
        return await runtime.tools.execute_async(call, context=context, skill_tools=None)


# ---------------------------------------------------------------------------
# T0: registration, visibility, identity, strict parameters
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = {
    "create_plan_draft", "modify_plan_document", "activate_goal_plan", "query_goals",
    "get_today_tasks", "get_action_context", "get_plan", "record_action_feedback", "defer_action",
}


def test_goal_tools_register_into_the_registry_and_model_snapshot(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch)
    names = {item["function"]["name"] for item in runtime.tools.describe()}
    assert EXPECTED_TOOLS <= names
    # The persisted behavior-bundle digest is computed after registration.
    manifest = runtime.behavior.active("stable").manifest
    assert "tools" in manifest


def test_goal_tools_can_be_disabled_for_new_runs(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path, monkeypatch, enabled=False)
    names = {item["function"]["name"] for item in runtime.tools.describe()}
    assert not (EXPECTED_TOOLS & names)


def test_phase_whitelist_opens_reads_everywhere_and_writes_in_react(tmp_path, monkeypatch):
    runtime = _tool_runtime(tmp_path)
    react = runtime._skill_tools_for("react")
    planning = runtime._skill_tools_for("planning")
    assert EXPECTED_TOOLS <= react
    assert {"query_goals", "get_today_tasks", "get_action_context", "get_plan"} <= planning
    assert not ({"create_plan_draft", "activate_goal_plan", "record_action_feedback", "defer_action"} & planning)


def test_model_supplied_identity_and_invalid_arguments_are_rejected(tmp_path, monkeypatch):
    from app.tools import ToolCall, ToolRejected

    runtime = _tool_runtime(tmp_path)
    with pytest.raises(ToolRejected, match="identity parameters"):
        runtime.tools.authorize(
            ToolCall("c1", "query_goals", {"owner_id": "someone-else"}),
            run_id="run-1", skill_tools=None,
        )
    rejected = "identity parameters|invalid arguments|unknown tool parameter"
    with pytest.raises(ToolRejected, match=rejected):
        runtime.tools.authorize(
            ToolCall("c2", "get_today_tasks", {"limit": True}),
            run_id="run-1", skill_tools=None,
        )
    with pytest.raises(ToolRejected, match=rejected):
        runtime.tools.authorize(
            ToolCall("c3", "get_today_tasks", {"unexpected": 1}),
            run_id="run-1", skill_tools=None,
        )
    with pytest.raises(ToolRejected, match=rejected):
        runtime.tools.authorize(
            ToolCall("c4", "defer_action", {"action_id": "a", "expected_version": 1, "scheduled_date": "not-a-date"}),
            run_id="run-1", skill_tools=None,
        )


def test_unresolved_run_identity_fails_closed(tmp_path, monkeypatch):
    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("无身份")
    # Direct handler invocation must not fall back to a default owner.
    result = _handler(runtime, "query_goals")({}, _context(runtime, thread.id, owner=""))
    assert result.ok is False and result.error == "RESOURCE_NOT_FOUND"


def test_write_tools_require_a_granted_approval(tmp_path, monkeypatch):
    from app.domain import ApprovalRequired
    from app.tools import ToolCall

    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("审批")
    context = _context(runtime, thread.id, run="run-approve", call="call-approve")
    call = ToolCall("call-approve", "create_plan_draft", {
        "title": "审批草稿", "markdown_content": "# 审批草稿\n\n- 第一步",
    })

    async def run_without_grant():
        with pytest.raises(ApprovalRequired):
            await runtime.tools.execute_async(call, context=context, skill_tools=None)

    asyncio.run(run_without_grant())

    approval = runtime.approvals.request(context.run_id, call.id, call.name, call.params)
    runtime.approvals.grant(approval.id, context.run_id, call.id, call.params)

    async def run_with_grant():
        return await runtime.tools.execute_async(call, context=context, skill_tools=None)

    result = asyncio.run(run_with_grant())
    assert result.ok is True and result.data["lifecycle"] == "draft"


def test_a_tool_call_id_cannot_be_reused_by_another_run(tmp_path):
    from app.tools import ToolCall, ToolRejected

    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("调用身份")
    call = ToolCall("shared-call", "query_goals", {})
    first_context = _context(runtime, thread.id, run="run-a", call="shared-call")
    second_context = _context(runtime, thread.id, run="run-b", call="shared-call")

    first = asyncio.run(runtime.tools.execute_async(call, context=first_context, skill_tools=None))
    assert first.ok is True
    with pytest.raises(ToolRejected, match="binding changed"):
        asyncio.run(runtime.tools.execute_async(call, context=second_context, skill_tools=None))


def test_skill_intersection_can_only_narrow_the_goal_tool_allowlist(tmp_path):
    runtime = _tool_runtime(tmp_path)
    no_skill = type("Run", (), {"id": "run-skill", "skill_names": ()})()
    assert EXPECTED_TOOLS <= runtime._skill_tools_for_run(no_skill, "react")
    # A run whose skill binding cannot be resolved fails closed: no goal tools.
    unbound = type("Run", (), {"id": "run-skill-missing", "skill_names": ("missing-skill",)})()
    assert runtime._skill_tools_for_run(unbound, "react") == set()


# ---------------------------------------------------------------------------
# P1: create_plan_draft
# ---------------------------------------------------------------------------

def test_create_plan_draft_is_create_only_and_never_activates(tmp_path, monkeypatch):
    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("草稿")
    first = _handler(runtime, "create_plan_draft")(
        {"title": "训练草稿", "markdown_content": "# 训练草稿\n\n- 慢跑 20 分钟"},
        _context(runtime, thread.id, run="run-draft", call="call-draft-1"),
    )
    assert first.ok is True
    assert first.data["lifecycle"] == "draft" and first.data["activated"] is False
    assert first.data["document_id"].startswith("plan_")

    second = _handler(runtime, "create_plan_draft")(
        {"title": "训练草稿", "markdown_content": "# 训练草稿\n\n- 慢跑 20 分钟"},
        _context(runtime, thread.id, run="run-draft", call="call-draft-2"),
    )
    assert second.ok is False and second.error == "PLAN_ALREADY_EXISTS"

    # No goal program or scheduled action was created by a draft save.
    assert runtime.goal_programs.list(owner_id="local-user") == []


def test_create_plan_draft_rejects_a_foreign_thread(tmp_path, monkeypatch):
    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("私有", owner_id="alice")
    result = _handler(runtime, "create_plan_draft")(
        {"title": "越权", "markdown_content": "# 越权\n\n- 无"},
        _context(runtime, thread.id, owner="bob"),
    )
    assert result.ok is False and result.error == "RESOURCE_NOT_FOUND"


# ---------------------------------------------------------------------------
# P2 + main chain: draft -> preview -> activate -> read -> feedback
# ---------------------------------------------------------------------------

async def _draft_and_preview(runtime, thread_id):
    draft = _handler(runtime, "create_plan_draft")(
        {"title": "一周计划", "markdown_content": "# 一周计划\n\n- 每天练习"},
        _context(runtime, thread_id, run="run-main", call="call-draft"),
    )
    assert draft.ok, draft
    preview = await _handler(runtime, "activate_goal_plan")(
        {
            "mode": "preview",
            "document_id": draft.data["document_id"],
            "expected_document_version_id": draft.data["version_id"],
            "start_date": "2026-09-01",
            "end_date": "2026-09-05",
            "timezone": "Asia/Shanghai",
            "daily_minutes": 60,
        },
        _context(runtime, thread_id, run="run-main", call="call-preview"),
    )
    assert preview.ok, preview
    return draft, preview


@pytest.mark.asyncio
async def test_preview_does_not_activate_and_requires_confirmation(tmp_path, monkeypatch):
    # async test: handlers under test include the async preview/activate adapter
    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("预览")
    draft, preview = await _draft_and_preview(runtime, thread.id)
    assert preview.data["activated"] is False
    program = runtime.goal_programs.get(preview.data["program_id"], owner_id="local-user")
    assert program["status"] == "DRAFT" and runtime.goal_programs.list(owner_id="local-user")[0]["status"] == "DRAFT"

    # A program compiled directly by the service (no preview confirmation
    # recorded) cannot be activated through the tool.
    direct = await runtime.goal_programs.preview(
        draft.data["document_id"], start_date="2026-09-01", timezone_name="Asia/Shanghai",
        daily_minutes=60, requested_end_date="2026-09-05", idempotency_key="direct-preview",
    )
    unconfirmed = await _handler(runtime, "activate_goal_plan")(
        {"mode": "activate", "program_id": direct["id"], "expected_version": direct["version"]},
        _context(runtime, thread.id, run="run-main", call="call-activate-x"),
    )
    assert unconfirmed.ok is False and unconfirmed.error == "ACTION_NOT_ELIGIBLE"


@pytest.mark.asyncio
async def test_confirmed_activation_then_reads_and_feedback(tmp_path, monkeypatch):
    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("主链路")
    draft, preview = await _draft_and_preview(runtime, thread.id)

    activated = await _handler(runtime, "activate_goal_plan")(
        {"mode": "activate", "program_id": preview.data["program_id"], "expected_version": preview.data["program_version"]},
        _approved_activation_context(runtime, thread.id, {"mode": "activate", "program_id": preview.data["program_id"], "expected_version": preview.data["program_version"]}),
    )
    assert activated.ok, activated
    assert activated.data["status"] == "ACTIVE" and activated.data["action_count"] >= 1

    goals = _handler(runtime, "query_goals")({}, _context(runtime, thread.id, run="run-main", call="call-goals"))
    assert goals.ok and goals.data["items"][0]["id"] == preview.data["program_id"]

    detail = _handler(runtime, "query_goals")(
        {"program_id": preview.data["program_id"]},
        _context(runtime, thread.id, run="run-main", call="call-goals-2"),
    )
    assert detail.ok and detail.data["item"]["action_count"] >= 1

    today = _handler(runtime, "get_today_tasks")(
        {"date": "2026-09-01"},
        _context(runtime, thread.id, run="run-main", call="call-today"),
    )
    assert today.ok and today.data["items"]
    assert today.data["items"][0]["category"] == "today"
    action_id = today.data["items"][0]["id"]

    context_view = _handler(runtime, "get_action_context")(
        {"action_id": action_id},
        _context(runtime, thread.id, run="run-main", call="call-action"),
    )
    assert context_view.ok and context_view.data["action"]["id"] == action_id
    assert context_view.data["action"]["timezone"] == "Asia/Shanghai"

    plan = _handler(runtime, "get_plan")(
        {"program_id": preview.data["program_id"], "version_mode": "goal_source"},
        _context(runtime, thread.id, run="run-main", call="call-plan"),
    )
    assert plan.ok and "# 一周计划" in plan.data["markdown"]
    assert plan.data["version_id"] == preview.data["source_document_version_id"]

    feedback = _handler(runtime, "record_action_feedback")(
        {"action_id": action_id, "expected_version": 0, "kind": "partial", "actual_minutes": 30, "note": "做了一半"},
        _context(runtime, thread.id, run="run-main", call="call-feedback"),
    )
    assert feedback.ok, feedback
    assert feedback.data["action_id"] == action_id
    refreshed = runtime.goal_programs.get_action_context(action_id, owner_id="local-user")["action"]
    assert refreshed["status"] == "SCHEDULED"  # progress recorded, not completed
    assert refreshed["progress"]["state"] == "PARTIAL"

    # Same tool call replays the business receipt without a second feedback row.
    replay = _handler(runtime, "record_action_feedback")(
        {"action_id": action_id, "expected_version": 0, "kind": "partial", "actual_minutes": 30, "note": "做了一半"},
        _context(runtime, thread.id, run="run-main", call="call-feedback"),
    )
    assert replay.ok and replay.data["receipt"]["id"] == feedback.data["receipt"]["id"]
    with runtime.db.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM goal_action_feedback WHERE action_id=?", (action_id,)).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# D4: modify an existing plan document
# ---------------------------------------------------------------------------

async def _draft_document(runtime, thread_id, *, call="call-draft"):
    from app.tools import ToolCall

    return await _execute_with_approval(
        runtime,
        ToolCall(call, "create_plan_draft", {
            "title": "一周计划", "markdown_content": "# 一周计划\n\n- 每天练习",
        }),
        _context(runtime, thread_id, run="run-doc", call=call),
    )


@pytest.mark.asyncio
async def test_modify_plan_document_creates_one_new_version_with_approval(tmp_path):
    from app.tools import ToolCall

    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("修改文档")
    draft = await _draft_document(runtime, thread.id)
    assert draft.ok, draft

    params = {
        "document_id": draft.data["document_id"],
        "expected_version_id": draft.data["version_id"],
        "title": "一周计划（减量）",
        "markdown_content": "# 一周计划\n\n- 每天练习 20 分钟",
    }
    result = await _execute_with_approval(
        runtime, ToolCall("call-modify", "modify_plan_document", params),
        _context(runtime, thread.id, run="run-doc", call="call-modify"),
    )
    assert result.ok, result
    assert result.data["version"] == 2
    assert result.data["execution_updated"] is False
    stored = runtime.plan_documents.get_version(result.data["version_id"])
    assert stored.status == "committed"
    assert "20 分钟" in stored.markdown_content

    # Replaying the same tool call returns the same committed version.
    replay = _handler(runtime, "modify_plan_document")(
        params, _context(runtime, thread.id, run="run-doc", call="call-modify"),
    )
    assert replay.ok and replay.data["version_id"] == result.data["version_id"]
    with runtime.db.connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM plan_document_versions WHERE plan_document_id=?",
            (draft.data["document_id"],),
        ).fetchone()[0]
    assert count == 2


@pytest.mark.asyncio
async def test_modify_rejects_stale_version_missing_document_and_deleted_document(tmp_path):
    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("修改边界")
    draft = await _draft_document(runtime, thread.id)
    assert draft.ok, draft
    base = {
        "document_id": draft.data["document_id"],
        "expected_version_id": draft.data["version_id"],
        "title": "一周计划",
        "markdown_content": "# 一周计划\n\n- 每天练习",
    }

    stale = _handler(runtime, "modify_plan_document")(
        {**base, "expected_version_id": "plan_version_missing"},
        _context(runtime, thread.id, run="run-doc", call="call-stale"),
    )
    assert stale.ok is False and stale.error == "VERSION_CONFLICT"

    missing = _handler(runtime, "modify_plan_document")(
        {**base, "document_id": "plan_missing"},
        _context(runtime, thread.id, run="run-doc", call="call-missing"),
    )
    assert missing.ok is False and missing.error == "RESOURCE_NOT_FOUND"

    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE plan_documents SET deleted_at='2026-09-20T00:00:00+00:00', file_status='deleted' WHERE id=?",
            (draft.data["document_id"],),
        )
    deleted = _handler(runtime, "modify_plan_document")(
        base, _context(runtime, thread.id, run="run-doc", call="call-deleted"),
    )
    assert deleted.ok is False and deleted.error == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_modify_is_owner_scoped(tmp_path):
    runtime = _tool_runtime(tmp_path)
    owner_thread = runtime.conversation.create_thread("修改归属")
    draft = await _draft_document(runtime, owner_thread.id)
    assert draft.ok, draft

    foreign = _handler(runtime, "modify_plan_document")(
        {
            "document_id": draft.data["document_id"],
            "expected_version_id": draft.data["version_id"],
            "title": "越权修改",
            "markdown_content": "# 越权",
        },
        _context(runtime, owner_thread.id, run="run-other", call="call-other", owner="other-user"),
    )
    assert foreign.ok is False and foreign.error == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_modify_does_not_recompile_an_activated_program(tmp_path):
    from app.tools import ToolCall

    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("激活后修改")
    draft, preview = await _draft_and_preview(runtime, thread.id)
    activated = await _handler(runtime, "activate_goal_plan")(
        {"mode": "activate", "program_id": preview.data["program_id"], "expected_version": preview.data["program_version"]},
        _approved_activation_context(runtime, thread.id, {
            "mode": "activate", "program_id": preview.data["program_id"],
            "expected_version": preview.data["program_version"],
        }),
    )
    assert activated.ok, activated
    before = runtime.goal_programs.get(preview.data["program_id"], owner_id="local-user")

    result = await _execute_with_approval(
        runtime,
        ToolCall("call-modify-live", "modify_plan_document", {
            "document_id": draft.data["document_id"],
            "expected_version_id": draft.data["version_id"],
            "title": "一周计划（新版）",
            "markdown_content": "# 一周计划\n\n- 每天练习 15 分钟",
        }),
        _context(runtime, thread.id, run="run-doc", call="call-modify-live"),
    )
    assert result.ok, result

    after = runtime.goal_programs.get(preview.data["program_id"], owner_id="local-user")
    assert after["status"] == "ACTIVE"
    assert after["source_plan_document_version_id"] == before["source_plan_document_version_id"]
    assert after["source_plan_content_hash"] == before["source_plan_content_hash"]
    assert len(after["actions"]) == len(before["actions"])


@pytest.mark.asyncio
async def test_preview_rejects_a_stale_source_version(tmp_path, monkeypatch):
    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("版本竞态")
    draft, _preview = await _draft_and_preview(runtime, thread.id)
    stale = await _handler(runtime, "activate_goal_plan")(
        {
            "mode": "preview",
            "document_id": draft.data["document_id"],
            "expected_document_version_id": "plan_version_missing",
            "start_date": "2026-09-01",
            "end_date": "2026-09-05",
            "timezone": "Asia/Shanghai",
            "daily_minutes": 60,
        },
        _context(runtime, thread.id, run="run-main", call="call-preview-stale"),
    )
    assert stale.ok is False and stale.error == "VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_activation_rejects_a_schedule_that_changed_after_preview(tmp_path, monkeypatch):
    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("确认后变化")
    draft, preview = await _draft_and_preview(runtime, thread.id)
    program_id = preview.data["program_id"]
    # Simulate a stale confirmation whose source no longer matches the program
    # the user is about to activate.
    runtime.goal_programs.record_preview_snapshot(
        program_id, "sha256:stale",
        source_version_id="plan_version_stale", source_content_hash="sha256:stale",
        owner_id="local-user",
    )
    stale = await _handler(runtime, "activate_goal_plan")(
        {"mode": "activate", "program_id": program_id, "expected_version": preview.data["program_version"]},
        _approved_activation_context(runtime, thread.id, {"mode": "activate", "program_id": program_id, "expected_version": preview.data["program_version"]}, call="call-activate-stale"),
    )
    assert stale.ok is False and stale.error == "VERSION_CONFLICT"


# ---------------------------------------------------------------------------
# T5/T6: feedback and defer with the fixture compiler
# ---------------------------------------------------------------------------

def _fixture_service(tmp_path):
    from app.conversation import ConversationService
    from app.db import Database
    from app.goal_program_compiler import FixedGoalProgramCompiler
    from app.goal_programs import GoalProgramService

    db = Database(tmp_path / "goal-tools.db")
    conversation = ConversationService(db)
    thread = conversation.create_thread("工具链")
    version = conversation.plan_documents.save_model_revision(
        thread_id=thread.id, title="七天训练", markdown_content="# 七天训练\n\n- 每天",
        source_turn_id=None, source_message_id=None, actor="user",
    )
    service = GoalProgramService(
        db, FixedGoalProgramCompiler(fixture(daily_minutes=30)),
        plan_documents=conversation.plan_documents, conversation=conversation,
    )
    return db, service, conversation, thread, version


def _specs(service, plan_documents):
    from app.goal_tools import build_goal_tool_specs

    return {spec.name: spec for spec in build_goal_tool_specs(service, plan_documents)}


def _fixture_context(thread_id, *, run="run-fx", call="call-fx", owner="local-user"):
    from app.tools import ToolExecutionContext

    return ToolExecutionContext(
        owner_id=owner, run_id=run, tool_call_id=call, thread_id=thread_id,
        project_id=None, root_budget_id=None, runtime_bundle_id=None,
    )


def test_defer_creates_a_replacement_and_replays_once(tmp_path):
    _db, service, _conversation, thread, version = _fixture_service(tmp_path)
    specs = _specs(service, service.plan_documents)
    program = asyncio.run(service.preview(
        version.plan_document_id, start_date="2026-09-01", timezone_name="Asia/Shanghai",
        daily_minutes=60, requested_end_date="2026-09-07", idempotency_key="fx-preview",
    ))
    active = service.activate(program["id"], expected_version=program["version"], idempotency_key="fx-activate")
    action = active["actions"][0]

    handler = specs["defer_action"].context_handler
    context = _fixture_context(thread.id)
    result = handler(
        {"action_id": action["id"], "expected_version": 0, "scheduled_date": "2026-09-07"},
        context,
    )
    assert result.ok, result
    assert result.data["original_action"]["status"] == "DEFERRED"
    assert result.data["replacement_action"]["scheduled_date"] == "2026-09-07"
    replacement_id = result.data["replacement_action"]["id"]

    replay = handler(
        {"action_id": action["id"], "expected_version": 0, "scheduled_date": "2026-09-07"},
        context,
    )
    assert replay.ok and replay.data["replacement_action"]["id"] == replacement_id
    with service.db.connection() as connection:
        replacements = connection.execute(
            "SELECT COUNT(*) FROM goal_actions WHERE deferred_from_action_id=?", (action["id"],),
        ).fetchone()[0]
    assert replacements == 1


def test_defer_reports_daily_capacity_and_rest_days_with_stable_codes(tmp_path):
    _db, service, _conversation, thread, version = _fixture_service(tmp_path)
    specs = _specs(service, service.plan_documents)
    program = asyncio.run(service.preview(
        version.plan_document_id, start_date="2026-09-01", timezone_name="Asia/Shanghai",
        daily_minutes=60, requested_end_date="2026-09-07", idempotency_key="fx-preview-cap",
    ))
    active = service.activate(program["id"], expected_version=program["version"], idempotency_key="fx-activate-cap")
    action = active["actions"][0]
    handler = specs["defer_action"].context_handler
    # The target day already carries a 30-minute action with a 60-minute budget,
    # so moving another 30-minute action there is allowed (60 total) but moving
    # one more is not. First move action 0 to day 7, then try action 1.
    first = handler({"action_id": action["id"], "expected_version": 0, "scheduled_date": "2026-09-07"}, _fixture_context(thread.id, call="call-d1"))
    assert first.ok
    second_action = active["actions"][1]
    second = handler({"action_id": second_action["id"], "expected_version": 0, "scheduled_date": "2026-09-07"}, _fixture_context(thread.id, call="call-d2"))
    assert second.ok is False and second.error == "DAILY_CAPACITY_EXCEEDED"

    with service.db.connection() as connection:
        connection.execute(
            "UPDATE goal_programs SET schedule_constraints_json=? WHERE id=?",
            ('{"available_weekdays":[],"excluded_dates":["2026-09-02"]}', program["id"]),
        )
    rest = handler({"action_id": active["actions"][2]["id"], "expected_version": 0, "scheduled_date": "2026-09-02"}, _fixture_context(thread.id, call="call-d3"))
    assert rest.ok is False and rest.error == "INVALID_ARGUMENT"


def test_feedback_tool_never_completes_the_action(tmp_path):
    _db, service, _conversation, thread, version = _fixture_service(tmp_path)
    specs = _specs(service, service.plan_documents)
    program = asyncio.run(service.preview(
        version.plan_document_id, start_date="2026-09-01", timezone_name="Asia/Shanghai",
        daily_minutes=60, requested_end_date="2026-09-07", idempotency_key="fx-preview-fb",
    ))
    active = service.activate(program["id"], expected_version=program["version"], idempotency_key="fx-activate-fb")
    action = active["actions"][0]
    handler = specs["record_action_feedback"].context_handler
    result = handler(
        {"action_id": action["id"], "expected_version": 0, "kind": "correction", "actual_minutes": 15},
        _fixture_context(thread.id, call="call-fb"),
    )
    assert result.ok, result
    assert result.data["receipt"]["kind"] == "correction"
    refreshed = service.get_action_context(action["id"], owner_id="local-user")["action"]
    assert refreshed["status"] == "SCHEDULED"

    empty = handler(
        {"action_id": action["id"], "expected_version": 1, "kind": "correction"},
        _fixture_context(thread.id, call="call-fb-empty"),
    )
    assert empty.ok is False and empty.error == "INVALID_ARGUMENT"
