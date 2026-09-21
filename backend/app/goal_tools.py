"""Goal business tools (T0/P1/P2/T1-T6).

Thin adapters over the existing services. They do not re-implement goal SQL,
feedback, deferral or plan-document persistence; they resolve identity from the
trusted :class:`ToolExecutionContext`, validate strict parameters, call the
service with a harness-generated idempotency key, and map business exceptions
to stable tool error codes.

Model-supplied ``owner_id``/``run_id``/``idempotency_key`` are rejected before
approval or execution.
"""

from __future__ import annotations

import inspect
import logging
from datetime import date as _date
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .goal_programs import (
    GoalCompilationError,
    GoalProgramConflict,
    GoalProgramNotFound,
    GoalProgramService,
)
from .plan_calendar_check import PlanCalendarMismatch
from .plan_documents import (
    PlanDocumentConflict,
    PlanDocumentService,
    PlanDocumentValidationError,
)
from .tools import ToolExecutionContext, ToolRejected, ToolRegistry, ToolResult, ToolRisk, ToolSpec

log = logging.getLogger("better-agent.goal-tools")

STRICT = ConfigDict(extra="forbid", strict=True)

INVALID_ARGUMENT = "INVALID_ARGUMENT"
RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
VERSION_CONFLICT = "VERSION_CONFLICT"
ACTION_NOT_ELIGIBLE = "ACTION_NOT_ELIGIBLE"
DATE_NOT_ALLOWED = "DATE_NOT_ALLOWED"
DAILY_CAPACITY_EXCEEDED = "DAILY_CAPACITY_EXCEEDED"
INTERNAL_ERROR = "INTERNAL_ERROR"
PLAN_ALREADY_EXISTS = "PLAN_ALREADY_EXISTS"
COMPILATION_FAILED = "COMPILATION_FAILED"


# ---------------------------------------------------------------------------
# Parameter models (strict, extra fields forbidden)
# ---------------------------------------------------------------------------

def _iso_date(value: str) -> str:
    parsed = _date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be canonical YYYY-MM-DD")
    return value


class ScheduleConstraints(BaseModel):
    model_config = STRICT
    available_weekdays: list[int] = Field(default_factory=lambda: list(range(7)))
    excluded_dates: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> "ScheduleConstraints":
        if any(isinstance(item, bool) or not 0 <= item <= 6 for item in self.available_weekdays):
            raise ValueError("available_weekdays must be integers 0-6")
        for item in self.excluded_dates:
            _iso_date(item)
        return self


class CreatePlanDraftParams(BaseModel):
    model_config = STRICT
    title: str = Field(min_length=1, max_length=200)
    markdown_content: str = Field(min_length=1, max_length=30_000)


class ModifyPlanDocumentParams(BaseModel):
    model_config = STRICT
    document_id: str = Field(min_length=1)
    expected_version_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    markdown_content: str = Field(min_length=1, max_length=30_000)


class ActivateGoalPlanParams(BaseModel):
    model_config = STRICT
    mode: Literal["preview", "activate"]
    document_id: str | None = None
    expected_document_version_id: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    timezone: str | None = None
    daily_minutes: int | None = None
    constraints: ScheduleConstraints | None = None
    program_id: str | None = None
    expected_version: int | None = None

    @model_validator(mode="after")
    def _validate(self) -> "ActivateGoalPlanParams":
        if self.mode == "preview":
            required = ("document_id", "expected_document_version_id", "start_date", "end_date", "timezone", "daily_minutes")
            missing = [name for name in required if getattr(self, name) is None]
            if missing:
                raise ValueError(f"preview requires {', '.join(missing)}")
            _iso_date(self.start_date)
            _iso_date(self.end_date)
            if isinstance(self.daily_minutes, bool) or not 5 <= self.daily_minutes <= 1440:
                raise ValueError("daily_minutes must be between 5 and 1440")
            if self.program_id is not None or self.expected_version is not None:
                raise ValueError("preview must not carry activate fields")
        else:
            if self.program_id is None or self.expected_version is None:
                raise ValueError("activate requires program_id and expected_version")
            if any(getattr(self, name) is not None for name in (
                "document_id", "expected_document_version_id", "start_date", "end_date",
                "timezone", "daily_minutes", "constraints",
            )):
                raise ValueError("activate must not carry preview fields")
            if isinstance(self.expected_version, bool) or self.expected_version < 0:
                raise ValueError("expected_version must be a non-negative integer")
        return self


class QueryGoalsParams(BaseModel):
    model_config = STRICT
    program_id: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)


class GetTodayTasksParams(BaseModel):
    model_config = STRICT
    program_id: str | None = None
    date: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def _validate(self) -> "GetTodayTasksParams":
        if self.date is not None:
            _iso_date(self.date)
        return self


class GetActionContextParams(BaseModel):
    model_config = STRICT
    action_id: str = Field(min_length=1)


class GetPlanParams(BaseModel):
    model_config = STRICT
    program_id: str | None = None
    version_mode: Literal["goal_source", "latest"] = "goal_source"
    document_id: str | None = None
    expected_version_id: str | None = None
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8000, ge=500, le=16_000)

    @model_validator(mode="after")
    def _validate(self) -> "GetPlanParams":
        if (self.program_id is None) == (self.document_id is None):
            raise ValueError("exactly one of program_id or document_id is required")
        if self.program_id is not None and self.expected_version_id is not None:
            raise ValueError("expected_version_id is only valid with document_id")
        return self


class RecordActionFeedbackParams(BaseModel):
    model_config = STRICT
    action_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    kind: Literal["partial", "correction"]
    actual_date: str | None = None
    actual_minutes: int | None = Field(default=None, ge=0, le=1440)
    difficulty: int | None = Field(default=None, ge=1, le=5)
    note: str | None = Field(default=None, max_length=2000)
    completed_work: str | None = Field(default=None, max_length=2000)
    remaining_work: str | None = Field(default=None, max_length=2000)
    output: str | None = Field(default=None, max_length=2000)
    cleared_fields: list[str] | None = None

    @model_validator(mode="after")
    def _validate(self) -> "RecordActionFeedbackParams":
        if self.actual_date is not None:
            _iso_date(self.actual_date)
        content_fields = ("actual_minutes", "difficulty", "note", "completed_work", "remaining_work", "output")
        has_content = any(getattr(self, name) is not None for name in content_fields)
        cleared = list(self.cleared_fields or [])
        if not has_content and not cleared:
            raise ValueError("feedback needs at least one field or cleared field")
        if cleared and self.kind != "correction":
            raise ValueError("only correction may clear fields")
        allowed = {"actual_minutes", "difficulty", "reason_code", "note", "completed_work", "remaining_work", "output"}
        if any(not isinstance(name, str) or name not in allowed for name in cleared):
            raise ValueError("cleared_fields contains an unsupported field")
        if set(cleared) & {name for name in content_fields if getattr(self, name) is not None}:
            raise ValueError("cannot set and clear the same field")
        return self


class DeferActionParams(BaseModel):
    model_config = STRICT
    action_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    scheduled_date: str

    @model_validator(mode="after")
    def _validate(self) -> "DeferActionParams":
        _iso_date(self.scheduled_date)
        return self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_key(context: ToolExecutionContext) -> str:
    return f"goal-tool:{context.run_id}:{context.tool_call_id}"


def _ok(summary: str, data: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(True, summary, data or {})


def _err(code: str, summary: str, data: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(False, summary, data or {}, error=code)


def _validate(model: type[BaseModel], params: dict[str, Any]) -> BaseModel:
    """Registry-side validator: raises ToolRejected with a stable prefix."""
    try:
        return model.model_validate(params)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        detail = f"{'.'.join(str(part) for part in first.get('loc', ()))}: {first.get('msg', 'invalid')}"
        raise ToolRejected(f"invalid arguments: {detail}") from exc


def _failure(exc: Exception, *, tool: str, context: ToolExecutionContext) -> ToolResult:
    if isinstance(exc, GoalProgramNotFound):
        return _err(RESOURCE_NOT_FOUND, "资源不存在或无权访问")
    if isinstance(exc, KeyError):
        # plan_documents reads raise KeyError for a missing version/document.
        return _err(RESOURCE_NOT_FOUND, "资源不存在或无权访问")
    if isinstance(exc, GoalProgramConflict):
        code = getattr(exc, "code", "GOAL_PROGRAM_CONFLICT")
        data = {"current": exc.current} if getattr(exc, "current", None) is not None else {}
        if code == DAILY_CAPACITY_EXCEEDED:
            return _err(DAILY_CAPACITY_EXCEEDED, str(exc), data)
        if code == ACTION_NOT_ELIGIBLE:
            return _err(ACTION_NOT_ELIGIBLE, str(exc), data)
        return _err(VERSION_CONFLICT, str(exc), data)
    if isinstance(exc, PlanDocumentConflict):
        if "PLAN_ALREADY_EXISTS" in str(exc):
            return _err(PLAN_ALREADY_EXISTS, "当前会话已有计划文档；不会覆盖或恢复已删除内容")
        return _err(VERSION_CONFLICT, str(exc))
    if isinstance(exc, GoalCompilationError):
        return _err(COMPILATION_FAILED, "计划编译失败，请调整草稿后重试", {"reason_code": exc.code})
    if isinstance(exc, (PlanDocumentValidationError, PlanCalendarMismatch, ValueError)):
        return _err(INVALID_ARGUMENT, str(exc))
    log.exception("goal tool failed tool=%s run=%s call=%s", tool, context.run_id, context.tool_call_id)
    return _err(INTERNAL_ERROR, "工具执行失败，请稍后重试")


def _thread_scope(service: GoalProgramService, owner_id: str, thread_id: str):
    with service.db.connection() as connection:
        row = connection.execute(
            "SELECT owner_id,project_id FROM threads WHERE id=? AND deleted_at IS NULL", (thread_id,),
        ).fetchone()
    if row is None or row["owner_id"] != owner_id:
        return None
    return row


def _check_thread_scope(service, thread_id, context):
    scope = _thread_scope(service, context.owner_id, thread_id)
    if scope is None or (context.project_id is not None and scope["project_id"] != context.project_id):
        raise GoalProgramNotFound(thread_id)


def _check_program_scope(service, program_id, context):
    with service.db.connection() as connection:
        program = connection.execute(
            "SELECT source_thread_id,deleted_at FROM goal_programs WHERE id=? AND owner_id=?",
            (program_id, context.owner_id),
        ).fetchone()
    if program is None or program["deleted_at"]:
        raise GoalProgramNotFound(program_id)
    _check_thread_scope(service, program["source_thread_id"], context)


def _check_tool_scope(service, documents, params, context):
    if not context.owner_id:
        raise GoalProgramNotFound("owner")
    if context.thread_id:
        _check_thread_scope(service, context.thread_id, context)
    if params.get("program_id"):
        _check_program_scope(service, params["program_id"], context)
    if params.get("action_id"):
        action = service.get_action_context(params["action_id"], owner_id=context.owner_id)["action"]
        _check_program_scope(service, action["program_id"], context)
    if params.get("document_id"):
        try:
            doc = documents.get_document(params["document_id"])
        except KeyError as exc:
            raise GoalProgramNotFound(params["document_id"]) from exc
        _check_thread_scope(service, doc.thread_id, context)


def activation_approval_binding(service, documents, params, context):
    """Snapshot displayed by the approval UI; generated preview is not consent."""
    if params.get("mode") != "activate":
        return {}
    _check_tool_scope(service, documents, params, context)
    snapshot = service.preview_snapshot(params["program_id"], context.owner_id)
    if snapshot is None:
        raise ToolRejected("execution preview required before activation")
    return {"goal_activation": snapshot}


def _program_list_view(program: dict[str, Any]) -> dict[str, Any]:
    return {
        key: program.get(key) for key in (
            "id", "objective_title", "objective_summary", "status", "timezone",
            "start_date", "end_date", "version", "compile_status", "progress",
        )
    }


def _program_detail_view(program: dict[str, Any]) -> dict[str, Any]:
    view = _program_list_view(program)
    view.update({
        "source_thread_id": program.get("source_thread_id"),
        "source_plan_document_id": program.get("source_plan_document_id"),
        "source_plan_document_version_id": program.get("source_plan_document_version_id"),
        "current_program_version_id": program.get("current_program_version_id"),
        "action_count": len(program.get("actions") or []),
        "deleted_at": program.get("deleted_at"),
    })
    return view


def _action_view(action: dict[str, Any]) -> dict[str, Any]:
    return {
        key: action.get(key) for key in (
            "id", "program_id", "scheduled_date", "position", "title", "description",
            "estimated_minutes", "completion_criteria", "status", "version",
            "required", "progress", "deferred_from_action_id",
        )
    }


def _task_view(item: dict[str, Any], *, category: str, program_id: str, timezone: str, local_date: str) -> dict[str, Any]:
    view = _action_view(item)
    view.update({
        "category": category,
        "program_id": program_id,
        "timezone": timezone,
        "local_date": local_date,
        "actual_minutes": (item.get("time_entry") or {}).get("actual_minutes"),
    })
    return view


def _confirmation_hash(program: dict[str, Any]) -> str:
    return GoalProgramService.activation_snapshot_hash(program)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _create_plan_draft(service: GoalProgramService, plan_documents: PlanDocumentService):
    def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = CreatePlanDraftParams.model_validate(params)
            if not context.thread_id:
                return _err(ACTION_NOT_ELIGIBLE, "当前运行没有可归属的会话上下文，无法保存计划草稿")
            scope = _thread_scope(service, context.owner_id, context.thread_id)
            if scope is None:
                return _err(RESOURCE_NOT_FOUND, "会话不存在或无权访问")
            version = plan_documents.save_model_revision(
                thread_id=context.thread_id,
                title=model.title,
                markdown_content=model.markdown_content,
                source_turn_id=None,
                source_message_id=None,
                actor="model",
                change_summary="goal tool draft",
                create_only=True,
                operation_key=_tool_key(context),
                owner_check=lambda: _assert_thread_owner(service, context.owner_id, context.thread_id),
            )
            document = plan_documents.get_document(version.plan_document_id)
            return _ok(
                "计划草稿已保存，尚未激活执行目标。",
                {
                    "document_id": version.plan_document_id,
                    "version_id": version.id,
                    "content_hash": version.content_hash,
                    "title": version.title,
                    "lifecycle": "draft",
                    "follow_up_enabled": False,
                    "delivery_ready": True,
                    "activated": False,
                    "file_status": document.file_status,
                },
            )
        except Exception as exc:  # noqa: BLE001 - mapped to stable codes
            return _failure(exc, tool="create_plan_draft", context=context)
    return handler


def _assert_thread_owner(service: GoalProgramService, owner_id: str, thread_id: str) -> None:
    if _thread_scope(service, owner_id, thread_id) is None:
        raise GoalProgramNotFound(thread_id)


def _modify_plan_document(service: GoalProgramService, plan_documents: PlanDocumentService):
    def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = ModifyPlanDocumentParams.model_validate(params)
            try:
                document = plan_documents.get_document(model.document_id)
            except KeyError:
                return _err(RESOURCE_NOT_FOUND, "计划文档不存在或无权访问")
            if document.deleted_at is not None:
                # Modification never resurrects a deleted document.
                return _err(RESOURCE_NOT_FOUND, "计划文档已被删除，不能通过工具恢复")
            scope = _thread_scope(service, context.owner_id, document.thread_id)
            if scope is None or (context.project_id is not None and scope["project_id"] != context.project_id):
                return _err(RESOURCE_NOT_FOUND, "计划文档不存在或无权访问")
            try:
                current = plan_documents.get_version(model.expected_version_id)
            except KeyError:
                return _err(VERSION_CONFLICT, "指定版本不存在或已变化，请重新读取计划文档")
            if current.plan_document_id != document.id:
                return _err(VERSION_CONFLICT, "指定版本不属于该计划文档，请重新读取")
            version = plan_documents.save_model_revision(
                thread_id=document.thread_id,
                title=model.title,
                markdown_content=model.markdown_content,
                source_turn_id=None,
                source_message_id=None,
                actor="model",
                expected_version_id=model.expected_version_id,
                expected_file_hash=current.content_hash,
                change_summary="goal tool modify",
                operation_key=_tool_key(context),
                owner_check=lambda: _assert_thread_owner(service, context.owner_id, document.thread_id),
            )
            refreshed = plan_documents.get_document(document.id)
            return _ok(
                "计划文档已更新新版本，尚未应用于执行安排。",
                {
                    "document_id": refreshed.id,
                    "version_id": version.id,
                    "version": version.version,
                    "content_hash": version.content_hash,
                    "title": version.title,
                    "file_status": refreshed.file_status,
                    "execution_updated": False,
                    "execution_note": "已激活目标继续绑定原来源版本；如需按新计划执行，请重新生成执行预览并确认。",
                },
            )
        except Exception as exc:  # noqa: BLE001 - mapped to stable codes
            return _failure(exc, tool="modify_plan_document", context=context)
    return handler


def _activate_goal_plan(service: GoalProgramService, plan_documents: PlanDocumentService):
    async def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = ActivateGoalPlanParams.model_validate(params)
            if model.mode == "preview":
                return await _preview_goal_plan(service, plan_documents, model, context)
            return _activate_goal_plan_now(service, model, context, params)
        except Exception as exc:  # noqa: BLE001 - mapped to stable codes
            return _failure(exc, tool="activate_goal_plan", context=context)
    return handler


async def _preview_goal_plan(
    service: GoalProgramService, plan_documents: PlanDocumentService,
    model: ActivateGoalPlanParams, context: ToolExecutionContext,
) -> ToolResult:
    document = _owned_document(service, plan_documents, context.owner_id, model.document_id)
    if document is None:
        return _err(RESOURCE_NOT_FOUND, "计划文档不存在或无权访问")
    try:
        version = plan_documents.get_version(model.expected_document_version_id)
    except KeyError:
        return _err(VERSION_CONFLICT, "指定版本不存在或已变化，请重新读取计划草稿")
    if version.plan_document_id != document.id:
        return _err(VERSION_CONFLICT, "指定版本不属于该计划文档，请重新读取")
    if version.status != "committed":
        return _err(VERSION_CONFLICT, "计划版本尚未提交，请重新保存后再编译")
    program = await service.preview(
        document.id,
        start_date=model.start_date,
        timezone_name=model.timezone,
        daily_minutes=model.daily_minutes,
        requested_end_date=model.end_date,
        idempotency_key=_tool_key(context),
        owner_id=context.owner_id,
        constraints=model.constraints.model_dump() if model.constraints is not None else None,
        expected_source_version_id=model.expected_document_version_id,
        root_budget_id=context.root_budget_id,
        runtime_bundle_id=context.runtime_bundle_id,
    )
    snapshot_hash = _confirmation_hash(program)
    service.record_preview_snapshot(
        program["id"], snapshot_hash,
        source_version_id=program["source_plan_document_version_id"],
        source_content_hash=program["source_plan_content_hash"],
        owner_id=context.owner_id,
    )
    actions = [
        {
            "logical_key": item.get("logical_key"),
            "scheduled_date": item.get("scheduled_date"),
            "position": item.get("position"),
            "title": item.get("title"),
            "estimated_minutes": item.get("estimated_minutes"),
            "required": item.get("required"),
        }
        for item in (program.get("structure") or {}).get("actions", [])
    ]
    return _ok(
        "已生成执行预览，请确认具体安排后再激活。",
        {
            "program_id": program["id"],
            "program_version": program["version"],
            "compile_status": program.get("compile_status"),
            "source_document_version_id": program.get("source_plan_document_version_id"),
            "source_plan_content_hash": program.get("source_plan_content_hash"),
            "start_date": program.get("start_date"),
            "end_date": program.get("end_date"),
            "timezone": program.get("timezone"),
            "daily_minutes": program.get("daily_minutes"),
            "calendar": program.get("calendar"),
            "actions": actions,
            "confirmation_snapshot_hash": snapshot_hash,
            "activated": False,
        },
    )


def _activate_goal_plan_now(
    service: GoalProgramService, model: ActivateGoalPlanParams, context: ToolExecutionContext, params: dict,
) -> ToolResult:
    from .domain import ApprovalService, ApprovalRequired
    binding = context.authorization or {}
    snapshot_hash = (binding.get("goal_activation") or {}).get("snapshot_hash")
    if not snapshot_hash:
        return _err(ACTION_NOT_ELIGIBLE, "需要用户明确确认执行预览；普通写入审批不能代替激活确认")
    try:
        ApprovalService(service.db).require_granted(context.run_id, context.tool_call_id,
                                                   params, binding)
    except ApprovalRequired:
        return _err(ACTION_NOT_ELIGIBLE, "执行安排尚未获得用户确认")
    cached = service.activation_receipt(model.program_id, expected_version=model.expected_version,
                                        idempotency_key=_tool_key(context), owner_id=context.owner_id)
    if cached is not None:
        return _activation_result(cached)
    program = service.get(model.program_id, owner_id=context.owner_id)
    confirmation = service.preview_snapshot(model.program_id, owner_id=context.owner_id)
    if confirmation is None:
        return _err(ACTION_NOT_ELIGIBLE, "该目标尚未生成并确认执行预览，不能直接激活")
    if (
        confirmation.get("snapshot_hash") != snapshot_hash
        or _confirmation_hash(program) != snapshot_hash
        or confirmation.get("program_version") != program.get("version")
        or confirmation.get("source_version_id") != program.get("source_plan_document_version_id")
        or confirmation.get("source_content_hash") != program.get("source_plan_content_hash")
    ):
        return _err(VERSION_CONFLICT, "预览之后目标或来源计划已变化，请重新编译预览并确认", {"current": _program_detail_view(program)})
    activated = service.activate(
        model.program_id, expected_version=model.expected_version,
        idempotency_key=_tool_key(context), owner_id=context.owner_id,
        expected_snapshot_hash=snapshot_hash,
    )
    return _activation_result(activated)


def _activation_result(activated):
    progress = activated.get("progress") or {}
    return _ok(
        "目标已激活，今日任务将在计划日期内生成。",
        {
            "program_id": activated["id"],
            "status": activated.get("status"),
            "version": activated.get("version"),
            "action_count": len(activated.get("actions") or []),
            "progress": progress,
        },
    )


def _owned_document(service: GoalProgramService, plan_documents: PlanDocumentService, owner_id: str, document_id: str):
    try:
        document = plan_documents.get_document(document_id)
    except KeyError:
        return None
    if _thread_scope(service, owner_id, document.thread_id) is None:
        return None
    return document


def _query_goals(service: GoalProgramService):
    def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = QueryGoalsParams.model_validate(params)
            if model.program_id:
                program = service.get(model.program_id, owner_id=context.owner_id)
                return _ok("已读取目标详情。", {"item": _program_detail_view(program)})
            page = service.list_summaries(
                owner_id=context.owner_id, offset=model.offset, limit=model.limit, project_id=context.project_id,
            )
            return _ok("已读取目标列表。", page)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, tool="query_goals", context=context)
    return handler


def _get_today_tasks(service: GoalProgramService):
    def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = GetTodayTasksParams.model_validate(params)
            if model.program_id:
                service.get(model.program_id, owner_id=context.owner_id)
            result = service.today(owner_id=context.owner_id, explicit_date=model.date)
            items: list[dict[str, Any]] = []
            group_dates: list[dict[str, Any]] = []
            for group in result["programs"]:
                if model.program_id and group["program"]["id"] != model.program_id:
                    continue
                program_id = group["program"]["id"]
                if context.project_id is not None:
                    try:
                        _check_program_scope(service, program_id, context)
                    except GoalProgramNotFound:
                        continue
                timezone = group["program"]["timezone"]
                local_date = group["local_date"]
                group_dates.append({"program_id": program_id, "local_date": local_date, "timezone": timezone})
                for category in ("today", "overdue", "completed"):
                    for item in group.get(category, []):
                        items.append(_task_view(item, category=category, program_id=program_id, timezone=timezone, local_date=local_date))
            items.sort(key=lambda item: (item["local_date"], item["scheduled_date"] or "", item["program_id"], item["position"] or 0, item["id"]))
            page = items[model.offset:model.offset + model.limit]
            next_offset = model.offset + model.limit
            return _ok(
                "已读取今日与逾期任务。",
                {
                    "items": page,
                    "has_more": next_offset < len(items),
                    "next_offset": next_offset if next_offset < len(items) else None,
                    "truncated": next_offset < len(items),
                    "groups": group_dates,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, tool="get_today_tasks", context=context)
    return handler


def _get_action_context(service: GoalProgramService):
    def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = GetActionContextParams.model_validate(params)
            context_data = service.get_action_context(model.action_id, owner_id=context.owner_id)
            action = context_data["action"]
            program = service.get(action["program_id"], owner_id=context.owner_id)
            view = _action_view(action)
            view.update({
                "program_summary": _program_list_view(program),
                "timezone": program.get("timezone"),
                "source_plan_document_id": program.get("source_plan_document_id"),
                "source_plan_document_version_id": program.get("source_plan_document_version_id"),
                "source_thread_id": program.get("source_thread_id"),
            })
            return _ok("已读取行动背景与最新版本。", {"action": view})
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, tool="get_action_context", context=context)
    return handler


def _get_plan(service: GoalProgramService, plan_documents: PlanDocumentService):
    def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = GetPlanParams.model_validate(params)
            if model.program_id:
                program = service.get(model.program_id, owner_id=context.owner_id)
                document = _owned_document(service, plan_documents, context.owner_id, program["source_plan_document_id"])
                if document is None:
                    return _err(RESOURCE_NOT_FOUND, "计划文档不存在或无权访问")
                if model.version_mode == "goal_source":
                    version_id = program["source_plan_document_version_id"]
                else:
                    version_id = document.current_version_id
                version = plan_documents.get_version(version_id)
                if version.plan_document_id != document.id:
                    return _err(VERSION_CONFLICT, "计划版本与目标来源不一致，请重新读取")
                source_version_id = program["source_plan_document_version_id"]
                differs = version.id != source_version_id
            else:
                document = _owned_document(service, plan_documents, context.owner_id, model.document_id)
                if document is None:
                    return _err(RESOURCE_NOT_FOUND, "计划文档不存在或无权访问")
                version = plan_documents.current_version(document.id)
                if model.expected_version_id is not None and version.id != model.expected_version_id:
                    return _err(VERSION_CONFLICT, "计划文档已更新，请重新读取", {"current_version_id": version.id})
                source_version_id = None
                differs = False
            markdown = version.markdown_content
            chunk = markdown[model.offset:model.offset + model.max_chars]
            next_offset = model.offset + model.max_chars
            has_more = next_offset < len(markdown)
            return _ok(
                "已读取计划文档片段。",
                {
                    "document_id": version.plan_document_id,
                    "version_id": version.id,
                    "version": version.version,
                    "title": version.title,
                    "content_hash": version.content_hash,
                    "version_mode": model.version_mode if model.program_id else "document",
                    "source_version_differs": differs,
                    "markdown": chunk,
                    "offset": model.offset,
                    "next_offset": next_offset if has_more else None,
                    "has_more": has_more,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, tool="get_plan", context=context)
    return handler


def _record_action_feedback(service: GoalProgramService):
    def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = RecordActionFeedbackParams.model_validate(params)
            payload: dict[str, Any] = {"kind": model.kind}
            for name in ("actual_minutes", "difficulty", "note", "completed_work", "remaining_work", "output", "actual_date"):
                value = getattr(model, name)
                if value is not None:
                    payload[name] = value
            if model.cleared_fields:
                payload["cleared_fields"] = list(model.cleared_fields)
            receipt = service.feedback(
                model.action_id, payload, expected_version=model.expected_version,
                idempotency_key=_tool_key(context), owner_id=context.owner_id,
            )
            action = service.get_action_context(model.action_id, owner_id=context.owner_id)["action"]
            return _ok(
                "已记录用户自报的进展，行动未被标记完成。",
                {
                    "receipt": receipt,
                    "action_id": model.action_id,
                    "current_snapshot": _action_view(action),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, tool="record_action_feedback", context=context)
    return handler


def _defer_action(service: GoalProgramService):
    def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        try:
            model = DeferActionParams.model_validate(params)
            result = service.defer_action(
                model.action_id, expected_version=model.expected_version,
                scheduled_date=model.scheduled_date,
                idempotency_key=_tool_key(context), owner_id=context.owner_id,
            )
            return _ok(
                "行动已延期，原行动保留为 DEFERRED，并创建了替代行动。",
                {
                    "original_action": _action_view(result["action"]),
                    "replacement_action": _action_view(result["replacement"]),
                    "progress": result.get("progress"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _failure(exc, tool="defer_action", context=context)
    return handler


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema(ref_template="#/$defs/{model}")


def _guard_identity(handler: Callable[..., Any], service: GoalProgramService, plan_documents: PlanDocumentService) -> Callable[..., Any]:
    """Fail closed when the run cannot resolve a trustworthy owner.

    The Runtime builds the context from the durable run/thread binding. If that
    binding is missing, goal tools refuse instead of falling back to a default
    owner.
    """
    if inspect.iscoroutinefunction(handler):
        async def async_wrapped(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
            if not context.owner_id:
                return _err(RESOURCE_NOT_FOUND, "运行记录缺少可确认的用户身份，无法使用目标工具")
            try:
                _check_tool_scope(service, plan_documents, params, context)
            except GoalProgramNotFound:
                return _err(RESOURCE_NOT_FOUND, "资源不存在或无权访问")
            return await handler(params, context)
        return async_wrapped

    def wrapped(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        if not context.owner_id:
            return _err(RESOURCE_NOT_FOUND, "运行记录缺少可确认的用户身份，无法使用目标工具")
        try:
            _check_tool_scope(service, plan_documents, params, context)
        except GoalProgramNotFound:
            return _err(RESOURCE_NOT_FOUND, "资源不存在或无权访问")
        return handler(params, context)
    return wrapped


def _recover_committed_draft(service, documents):
    def recover(params, context):
        _check_tool_scope(service, documents, params, context)
        if not context.thread_id:
            return None
        version_id = documents.operation_version_id(context.thread_id, _tool_key(context))
        try:
            version = documents.get_version(version_id)
        except KeyError:
            return None
        if version.status != "committed":
            return None
        # The committed branch validates the operation content and only reads.
        return _create_plan_draft(service, documents)(params, context)
    return recover


def _recover_committed_modification(service, documents):
    def recover(params, context):
        _check_tool_scope(service, documents, params, context)
        try:
            document = documents.get_document(params["document_id"])
        except KeyError:
            return None
        if document.deleted_at is not None:
            return None
        version_id = documents.operation_version_id(document.thread_id, _tool_key(context))
        try:
            version = documents.get_version(version_id)
        except KeyError:
            return None
        if version.status != "committed":
            return None
        return _modify_plan_document(service, documents)(params, context)
    return recover


def _recover_committed_activation(service, documents):
    def recover(params, context):
        _check_tool_scope(service, documents, params, context)
        model = ActivateGoalPlanParams.model_validate(params)
        if model.mode != "activate":
            return None
        cached = service.activation_receipt(model.program_id, expected_version=model.expected_version,
                                            idempotency_key=_tool_key(context), owner_id=context.owner_id)
        if cached is None:
            return None
        return _activate_goal_plan_now(service, model, context, params)
    return recover


def build_goal_tool_specs(
    goal_programs: GoalProgramService, plan_documents: PlanDocumentService,
) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="create_plan_draft",
            description=(
                "保存根据用户目标生成的计划草稿，供用户查看和确认。不会创建已激活的执行目标，也不会覆盖当前会话已有计划。"
                "保存成功只代表文档可交付，不代表开启执行管理、提醒或每日复盘；用户明确要求管理执行进度时再使用 activate_goal_plan。"
            ),
            schema=_schema(CreatePlanDraftParams),
            risk=ToolRisk.WRITE,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_create_plan_draft(goal_programs, plan_documents), goal_programs, plan_documents),
            recover_result=_recover_committed_draft(goal_programs, plan_documents),
            validator=lambda params: _validate(CreatePlanDraftParams, params),
            reject_identity_params=True,
            timeout_seconds=30,
        ),
        ToolSpec(
            name="modify_plan_document",
            description=(
                "修改已有计划文档并提交新版本，需要用户确认。只产生新版本，不会自动改写已激活的执行安排，也不会开启提醒或复盘；"
                "必须使用读取到的 document_id 与 expected_version_id，不能编造。"
            ),
            schema=_schema(ModifyPlanDocumentParams),
            risk=ToolRisk.WRITE,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_modify_plan_document(goal_programs, plan_documents), goal_programs, plan_documents),
            recover_result=_recover_committed_modification(goal_programs, plan_documents),
            validator=lambda params: _validate(ModifyPlanDocumentParams, params),
            reject_identity_params=True,
            timeout_seconds=30,
        ),
        ToolSpec(
            name="activate_goal_plan",
            description=(
                "将已保存的计划编译为执行预览，并在用户确认该预览后激活。preview 只生成待确认目标；activate 必须有系统验证的用户确认，不能自行跳过。"
                "activate 只传 mode、program_id、expected_version，不要携带 document_id、文档版本、日期或预览字段，携带会被拒绝。"
                "激活只建立行动安排和进度记录，不代表开启主动提醒、定时监督或自动复盘；未实际配置调度时不得声称已开启每日复盘。"
            ),
            schema=_schema(ActivateGoalPlanParams),
            risk=ToolRisk.WRITE,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_activate_goal_plan(goal_programs, plan_documents), goal_programs, plan_documents),
            recover_result=_recover_committed_activation(goal_programs, plan_documents),
            validator=lambda params: _validate(ActivateGoalPlanParams, params),
            reject_identity_params=True,
            timeout_seconds=180,
        ),
        ToolSpec(
            name="query_goals",
            description="查询当前用户的长期目标列表或指定目标详情。用户未明确目标时先查询候选；不要猜测目标 ID。",
            schema=_schema(QueryGoalsParams),
            risk=ToolRisk.READ,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_query_goals(goal_programs), goal_programs, plan_documents),
            validator=lambda params: _validate(QueryGoalsParams, params),
            reject_identity_params=True,
        ),
        ToolSpec(
            name="get_today_tasks",
            description="查询今日或指定日期的行动安排，同时区分逾期和已完成事项。默认日期按各目标所在时区确定。",
            schema=_schema(GetTodayTasksParams),
            risk=ToolRisk.READ,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_get_today_tasks(goal_programs), goal_programs, plan_documents),
            validator=lambda params: _validate(GetTodayTasksParams, params),
            reject_identity_params=True,
        ),
        ToolSpec(
            name="get_action_context",
            description="读取一个行动的目标背景、任务说明、完成标准、已有进展和最新版本。记录反馈或延期前应先读取。",
            schema=_schema(GetActionContextParams),
            risk=ToolRisk.READ,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_get_action_context(goal_programs), goal_programs, plan_documents),
            validator=lambda params: _validate(GetActionContextParams, params),
            reject_identity_params=True,
        ),
        ToolSpec(
            name="get_plan",
            description="读取目标关联的计划文档。默认读取生成该目标时绑定的计划版本；只有查看最新文档时选择 latest。内容分段返回，可继续读取。",
            schema=_schema(GetPlanParams),
            risk=ToolRisk.READ,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_get_plan(goal_programs, plan_documents), goal_programs, plan_documents),
            validator=lambda params: _validate(GetPlanParams, params),
            reject_identity_params=True,
        ),
        ToolSpec(
            name="record_action_feedback",
            description="记录用户明确陈述的行动进展或更正已有反馈，不标记行动完成。不得推测投入时间、难度和完成情况。调用前读取最新行动版本。",
            schema=_schema(RecordActionFeedbackParams),
            risk=ToolRisk.WRITE,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_record_action_feedback(goal_programs), goal_programs, plan_documents),
            validator=lambda params: _validate(RecordActionFeedbackParams, params),
            reject_identity_params=True,
        ),
        ToolSpec(
            name="defer_action",
            description="将待执行行动延期到用户指定的更晚日期。操作会保留原行动并创建替代行动；必须使用最新版本，并遵守目标日历和每日容量限制。",
            schema=_schema(DeferActionParams),
            risk=ToolRisk.WRITE,
            handler=lambda params: _err(INVALID_ARGUMENT, "该工具需要服务端执行上下文"),
            context_handler=_guard_identity(_defer_action(goal_programs), goal_programs, plan_documents),
            validator=lambda params: _validate(DeferActionParams, params),
            reject_identity_params=True,
        ),
    ]


def register_goal_tools(
    registry: ToolRegistry,
    *,
    goal_programs: GoalProgramService,
    plan_documents: PlanDocumentService,
) -> list[str]:
    specs = build_goal_tool_specs(goal_programs, plan_documents)
    for spec in specs:
        registry.register(spec)
    return [spec.name for spec in specs]
