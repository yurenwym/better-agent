"""Native conversation capabilities, with identity supplied by the harness."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .tools import ToolExecutionContext, ToolRegistry, ToolArgumentError, ToolResult, ToolRisk, ToolSpec


class ResearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    topic: str = Field(min_length=1, max_length=2000)
    scope: Literal["web", "local_note"] = "web"


class ExpertParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    objective: str = Field(min_length=1, max_length=2000)
    roles: list[Literal["researcher", "planner", "critic"]] = Field(min_length=1, max_length=3)

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, roles):
        return list(dict.fromkeys(roles))


class RememberParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["preference", "constraint", "fact", "decision", "lesson"]
    scope: Literal["global", "project"]
    content: str = Field(min_length=1, max_length=4000)


class PublishParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)
    source: Literal["this_response", "latest_assistant_plan"] = "this_response"


@dataclass(frozen=True)
class ConversationCapabilityBinding:
    worker: object
    turn: object
    owner_id: str
    context_incomplete: bool = False
    history: tuple = ()
    plan_context: object = None
    source_message_id: str | None = None
    content: str = ""
    bound_response: dict = field(default_factory=dict)


def _binding_matches(binding, context):
    turn = binding.turn
    return (context.owner_id == binding.owner_id and context.turn_id == turn.id
            and context.thread_id == turn.thread_id
            and context.runtime_bundle_id == turn.runtime_bundle_id
            and context.root_budget_id == turn.root_budget_id)


def _validator(model):
    def validate(params):
        try:
            model.model_validate(params)
        except ValidationError as exc:
            raise ToolArgumentError("INVALID_ARGUMENT") from exc
    return validate


def register_research_capability(
    registry: ToolRegistry,
    resolve: Callable[[ToolExecutionContext], ConversationCapabilityBinding],
) -> None:
    def execute(params, context):
        binding = resolve(context)
        turn = binding.turn
        if (context.owner_id != binding.owner_id or context.turn_id != turn.id
                or context.thread_id != turn.thread_id
                or context.runtime_bundle_id != turn.runtime_bundle_id
                or context.root_budget_id != turn.root_budget_id):
            return ToolResult(False, "调用身份与回合不一致", error="CONTEXT_IDENTITY_CONFLICT")
        try:
            parsed = ResearchParams.model_validate(params)
            job = binding.worker.handoff_start_research(
                turn, parsed.topic, parsed.scope, context_incomplete=binding.context_incomplete,
            )
            return ToolResult(True, "研究任务已创建", data={"job_id": job.id, "task_ref": {"kind": "research", "id": job.id}},
                              meta={"capability_kind": "handoff", "kind": "research", "ref": job.id})
        except (ValueError, RuntimeError, KeyError) as exc:
            return ToolResult(False, str(exc), error="HANDOFF_REJECTED")
    registry.register(ToolSpec(
        name="start_research", description="仅当用户明确要求启动深度研究时调用；一般解释、比较或分析直接回答。创建异步任务后结束当前回合。",
        schema=ResearchParams.model_json_schema(), risk=ToolRisk.READ,
        handler=lambda _: ToolResult(False, "需要可信回合上下文", error="CONTEXT_REQUIRED"),
        context_handler=execute, validator=_validator(ResearchParams), reject_identity_params=True,
    ))


def register_expert_capability(registry: ToolRegistry, resolve: Callable) -> None:
    def execute(params, context):
        binding = resolve(context)
        if not _binding_matches(binding, context):
            return ToolResult(False, "调用身份与回合不一致", error="CONTEXT_IDENTITY_CONFLICT")
        try:
            parsed = ExpertParams.model_validate(params)
            run = binding.worker.handoff_start_expert(
                binding.turn, parsed.objective, tuple(parsed.roles), content=binding.content,
                history=list(binding.history), plan_context=binding.plan_context,
                source_message_id=binding.source_message_id,
                reason_code="tool:delegate_experts", context_incomplete=binding.context_incomplete,
            )
            return ToolResult(True, "专家任务已创建", data={"run_id": run["id"], "task_ref": {"kind": "expert", "id": run["coordinator_task_id"]}},
                              meta={"capability_kind": "handoff", "kind": "expert", "ref": run["id"]})
        except (ValueError, RuntimeError, KeyError) as exc:
            return ToolResult(False, str(exc), error="HANDOFF_REJECTED")
    registry.register(ToolSpec(
        name="delegate_experts", description="仅当用户明确要求专家协作时调用。选择所需专家角色，创建异步任务后结束当前回合。",
        schema=ExpertParams.model_json_schema(), risk=ToolRisk.READ,
        handler=lambda _: ToolResult(False, "需要可信回合上下文", error="CONTEXT_REQUIRED"),
        context_handler=execute, validator=_validator(ExpertParams), reject_identity_params=True,
    ))


def register_remember_capability(registry: ToolRegistry, resolve: Callable) -> None:
    def execute(params, context):
        binding = resolve(context)
        if not _binding_matches(binding, context):
            return ToolResult(False, "调用身份与回合不一致", error="CONTEXT_IDENTITY_CONFLICT")
        parsed = RememberParams.model_validate(params)
        if binding.context_incomplete or not binding.source_message_id:
            return ToolResult(False, "缺少完整来源上下文", error="CONTEXT_REQUIRED")
        if parsed.scope == "project" and not context.project_id:
            return ToolResult(False, "项目记忆需要当前项目", error="PROJECT_REQUIRED")
        runtime = binding.worker.conversation.agent_runtime
        key = "conversation:" + hashlib.sha256((context.owner_id + ":" + binding.source_message_id + ":" + binding.content).encode()).hexdigest()
        try:
            item = runtime.memory_store.remember(
                context.owner_id, parsed.kind, "user" if parsed.scope == "global" else "project",
                context.project_id if parsed.scope == "project" else "", parsed.content, key,
                [{"source_type": "thread_message", "source_id": binding.source_message_id}],
                source_thread_id=context.thread_id,
            )
            return ToolResult(True, "已记住", data={"id": item.id, "content": item.content, "kind": parsed.kind, "scope": parsed.scope})
        except (ValueError, RuntimeError, KeyError) as exc:
            return ToolResult(False, str(exc), error="MEMORY_REJECTED")
    registry.register(ToolSpec(
        name="remember", description="仅当用户明确要求为未来对话记住稳定信息时调用。不得保存凭据、密钥或未经请求的推断。",
        schema=RememberParams.model_json_schema(), risk=ToolRisk.READ,
        handler=lambda _: ToolResult(False, "需要可信回合上下文", error="CONTEXT_REQUIRED"),
        context_handler=execute, validator=_validator(RememberParams), reject_identity_params=True,
    ))


def register_publish_capability(registry: ToolRegistry, resolve: Callable) -> None:
    def execute(params, context):
        from .live_model import _latest_assistant_plan
        from .plan_documents import normalize_markdown, validate_title
        from .conversation import _canonicalize_visible_markdown
        binding = resolve(context)
        if not _binding_matches(binding, context) or binding.context_incomplete:
            return ToolResult(False, "缺少可信完整上下文", error="CONTEXT_REQUIRED")
        parsed = PublishParams.model_validate(params)
        text = binding.bound_response.get("text")
        if parsed.source == "latest_assistant_plan":
            text = _latest_assistant_plan(list(binding.history))
        if not isinstance(text, str) or not text.strip():
            return ToolResult(False, "需要可绑定的计划正文", error="NEED_BOUND_TEXT")
        try:
            validate_title(parsed.title)
            canonical = normalize_markdown(_canonicalize_visible_markdown(text))
            from .plan_calendar_check import validate_plan_calendar
            validate_plan_calendar(canonical)
            service = binding.worker.conversation.plan_documents
            with service.db.connection() as connection:
                existing = connection.execute("SELECT id,current_version_id FROM plan_documents WHERE thread_id=?", (context.thread_id,)).fetchone()
            if existing:
                if existing["current_version_id"]:
                    version = service.get_version(existing["current_version_id"])
                    if (version.source_turn_id == context.turn_id and version.markdown_content == canonical
                            and version.title == parsed.title and version.status == "committed"):
                        return ToolResult(True, "计划已保存", data={"version_id": version.id, "title": parsed.title,
                            "text": canonical, "content_hash": version.content_hash}, meta={"capability_kind": "bind_text"})
                return ToolResult(False, "已有计划，调用 modify_plan_document 并等待用户审批。", data=dict(existing), error="PLAN_ALREADY_EXISTS")
            version = service.save_model_revision(thread_id=context.thread_id, title=parsed.title,
                markdown_content=canonical, source_turn_id=context.turn_id, source_message_id=None,
                actor="model", create_only=True, owner_check=lambda: binding.worker._assert_job_owner(context.turn_id))
            return ToolResult(True, "计划已保存", data={"version_id": version.id, "title": parsed.title,
                "text": canonical, "content_hash": version.content_hash}, meta={"capability_kind": "bind_text"})
        except (ValueError, RuntimeError, OSError) as exc:
            return ToolResult(False, str(exc), error="PLAN_REJECTED")
    registry.register(ToolSpec(name="publish_plan_document", description="用户要求保存新计划时，在同一响应中先输出完整 Markdown 正文再调用；保存刚才的计划使用 latest_assistant_plan。已有计划需 modify_plan_document 审批。",
        schema=PublishParams.model_json_schema(), risk=ToolRisk.READ,
        handler=lambda _: ToolResult(False, "需要可信正文绑定", error="CONTEXT_REQUIRED"),
        context_handler=execute, validator=_validator(PublishParams), reject_identity_params=True))
