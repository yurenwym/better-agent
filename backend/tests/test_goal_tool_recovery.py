"""Business recovery and consent tests shared with isolated PostgreSQL."""
import asyncio
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.domain import ApprovalService
from app.goal_program_compiler import FixedGoalProgramCompiler
from app.goal_programs import GoalProgramService
from app.goal_tools import activation_approval_binding, register_goal_tools, ScheduleConstraints
from app.tools import ToolCall, ToolExecutionContext, ToolRegistry
from test_goal_program_compiler import fixture


@pytest.fixture
def recovery_db(tmp_path):
    db = Database(tmp_path / "recovery.db")
    yield db
    db.close()


def setup(db, tmp_path):
    conversation = ConversationService(db)
    service = GoalProgramService(db, FixedGoalProgramCompiler(fixture(daily_minutes=30)),
                                 plan_documents=conversation.plan_documents, conversation=conversation)
    approvals = ApprovalService(db)
    tools = ToolRegistry(tmp_path / "artifacts", db, approvals)
    register_goal_tools(tools, goal_programs=service, plan_documents=conversation.plan_documents)
    thread = conversation.create_thread("旅游攻略")
    context = ToolExecutionContext(owner_id="local-user", run_id="test-goal-run", tool_call_id="draft",
                                   thread_id=thread.id)
    return conversation, service, tools, context


def grant(tools, call, context, binding=None):
    approval = tools.approval_service.request(context.run_id, call.id, call.name, call.params, binding=binding)
    tools.approval_service.grant(approval.id, context.run_id, call.id, call.params, binding=binding)


def execute(tools, call, context, binding=None):
    return asyncio.run(tools.execute_async(call, context=replace(context, tool_call_id=call.id),
                                           skill_tools=None, authorization=binding))


def draft_call():
    return ToolCall("draft", "create_plan_draft", {"title": "杭州旅行", "markdown_content": "# 杭州旅行\n\n第一天游览西湖，第二天参观博物馆。"})


def prepare_preview(service, tools, context):
    draft = draft_call()
    grant(tools, draft, context)
    saved = execute(tools, draft, context)
    assert saved.ok
    preview = ToolCall("preview", "activate_goal_plan", {
        "mode": "preview", "document_id": saved.data["document_id"],
        "expected_document_version_id": saved.data["version_id"],
        "start_date": "2026-09-01", "end_date": "2026-09-07", "timezone": "Asia/Shanghai", "daily_minutes": 60,
    })
    grant(tools, preview, context)
    compiled = execute(tools, preview, context)
    assert compiled.ok, compiled
    call = ToolCall("activate", "activate_goal_plan", {
        "mode": "activate", "program_id": compiled.data["program_id"],
        "expected_version": compiled.data["program_version"],
    })
    ctx = replace(context, tool_call_id=call.id)
    binding = activation_approval_binding(service, service.plan_documents, call.params, ctx)
    return saved, call, ctx, binding


def test_travel_delivery_recovers_without_creating_execution(recovery_db, tmp_path, monkeypatch):
    conversation, service, tools, context = setup(recovery_db, tmp_path)
    call = draft_call()
    grant(tools, call, context)
    original = tools._record_call
    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after commit")
    monkeypatch.setattr(tools, "_record_call", crash)
    with pytest.raises(RuntimeError, match="after commit"):
        execute(tools, call, context)
    version = conversation.plan_documents.get_by_thread(context.thread_id).current_version_id
    monkeypatch.setattr(tools, "_record_call", original)
    result = execute(tools, call, context)
    assert result.ok and result.data["version_id"] == version
    assert result.data["delivery_ready"] and not result.data["follow_up_enabled"]
    assert service.list() == []
    with recovery_db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_document_versions").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM goal_actions").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM goal_daily_reviews").fetchone()[0] == 0


def test_preview_is_not_user_consent(recovery_db, tmp_path):
    _, service, tools, context = setup(recovery_db, tmp_path)
    _, call, ctx, binding = prepare_preview(service, tools, context)
    assert binding["goal_activation"]["preview"]["structure"]["actions"]
    # A generic WRITE approval cannot activate a goal.
    grant(tools, call, ctx)
    result = execute(tools, call, ctx)
    assert not result.ok and result.error == "ACTION_NOT_ELIGIBLE"
    assert service.get(call.params["program_id"])["status"] == "DRAFT"


def test_activation_recovers_commit_before_tool_result(recovery_db, tmp_path, monkeypatch):
    _, service, tools, context = setup(recovery_db, tmp_path)
    _, call, ctx, binding = prepare_preview(service, tools, context)
    grant(tools, call, ctx, binding)
    original = tools._record_call
    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after activation")
    monkeypatch.setattr(tools, "_record_call", crash)
    with pytest.raises(RuntimeError, match="after activation"):
        execute(tools, call, ctx, binding)
    monkeypatch.setattr(tools, "_record_call", original)
    result = execute(tools, call, ctx, binding)
    assert result.ok and result.data["status"] == "ACTIVE"
    with recovery_db.connection() as c:
        count = c.execute("SELECT COUNT(*) FROM goal_actions").fetchone()[0]
    assert count == result.data["action_count"]
    assert execute(tools, call, ctx, binding).data == result.data


def test_changed_schedule_invalidates_approval(recovery_db, tmp_path):
    _, service, tools, context = setup(recovery_db, tmp_path)
    _, call, ctx, binding = prepare_preview(service, tools, context)
    grant(tools, call, ctx, binding)
    # No version increment: hash must catch changes that version-only guards miss.
    with recovery_db.transaction() as c:
        c.execute("UPDATE goal_programs SET daily_minutes=90 WHERE id=?", (call.params["program_id"],))
    result = execute(tools, call, ctx, binding)
    assert not result.ok and result.error == "VERSION_CONFLICT"
    assert service.get(call.params["program_id"])["status"] == "DRAFT"


def test_draft_operation_rejects_changed_content(recovery_db, tmp_path):
    _, service, tools, context = setup(recovery_db, tmp_path)
    handler = tools._tools["create_plan_draft"].context_handler
    assert handler(draft_call().params, context).ok
    changed = {**draft_call().params, "title": "不同计划"}
    assert handler(changed, context).error == "VERSION_CONFLICT"


def test_concurrent_draft_creation_has_one_version(recovery_db, tmp_path):
    _, service, tools, context = setup(recovery_db, tmp_path)
    handler = tools._tools["create_plan_draft"].context_handler
    def create(i):
        return handler(draft_call().params, replace(context, tool_call_id=f"parallel-{i}"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(r.ok for r in results) == 1
    assert {r.error for r in results if not r.ok} == {"PLAN_ALREADY_EXISTS"}
    with recovery_db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_document_versions").fetchone()[0] == 1


def test_optional_exclusions_keep_default_weekdays():
    from app.goal_calendar import schedule_constraints
    value = ScheduleConstraints.model_validate({"excluded_dates": ["2026-09-22"]})
    assert schedule_constraints(value.model_dump())["available_weekdays"] == list(range(7))


def test_project_scope_blocks_reads_and_writes(recovery_db, tmp_path):
    _, service, tools, context = setup(recovery_db, tmp_path)
    saved, call, ctx, binding = prepare_preview(service, tools, context)
    restricted = replace(ctx, project_id="unrelated-project", thread_id=None)
    queries = [
        ("get_plan", {"document_id": saved.data["document_id"]}),
        ("query_goals", {"program_id": call.params["program_id"]}),
        ("activate_goal_plan", call.params),
    ]
    for name, params in queries:
        result = tools._tools[name].context_handler(params, restricted)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        assert not result.ok and result.error == "RESOURCE_NOT_FOUND"
    page = tools._tools["query_goals"].context_handler({}, restricted)
    assert page.ok and page.data["items"] == []
    grant(tools, call, ctx, binding)
    assert execute(tools, call, ctx, binding).ok
    action = service.get(call.params["program_id"])["actions"][0]
    for name, params in (
        ("get_action_context", {"action_id": action["id"]}),
        ("record_action_feedback", {"action_id": action["id"], "expected_version": 0,
                                    "kind": "partial", "actual_minutes": 5}),
        ("defer_action", {"action_id": action["id"], "expected_version": 0, "scheduled_date": "2026-09-07"}),
    ):
        result = tools._tools[name].context_handler(params, restricted)
        assert not result.ok and result.error == "RESOURCE_NOT_FOUND"
    today = tools._tools["get_today_tasks"].context_handler({"date": "2026-09-01"}, restricted)
    assert today.ok and today.data["items"] == []


def test_negative_offsets_are_rejected():
    from pydantic import ValidationError
    from app.goal_tools import GetTodayTasksParams, GetPlanParams, QueryGoalsParams
    for model, params in ((GetTodayTasksParams, {}), (QueryGoalsParams, {}), (GetPlanParams, {"document_id": "x"})):
        with pytest.raises(ValidationError):
            model.model_validate({**params, "offset": -1})
