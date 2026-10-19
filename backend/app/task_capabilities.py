"""Conversation controls for existing research and expert tasks."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .conversation_capabilities import _validator
from .policy_adapters import decide_task_control
from .tool_contracts import ToolResult, ToolRisk, ToolSpec


class TaskParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["research", "expert"]
    id: str = Field(min_length=1, max_length=200)


def load_task_control(connection, kind: str, task_id: str):
    """The persisted owner and thread for a research job or an expert run.

    ``task_id`` is the research job id, the expert run id, or the coordinator
    task id. A deleted thread yields no row. The row is not proof of access.
    """
    if kind == "research":
        return connection.execute(
            "SELECT j.id AS task_id,j.thread_id,h.owner_id FROM research_jobs j "
            "JOIN threads h ON h.id=j.thread_id WHERE j.id=? AND h.deleted_at IS NULL",
            (task_id,),
        ).fetchone()
    return connection.execute(
        "SELECT r.id AS task_id,r.thread_id,r.owner_id FROM agent_runs r "
        "JOIN threads h ON h.id=r.thread_id AND h.owner_id=r.owner_id "
        "WHERE h.deleted_at IS NULL AND (r.id=? OR r.coordinator_task_id=?)",
        (task_id, task_id),
    ).fetchone()


def task_is_allowed(row, *, owner_id: str, caller_thread_id: str | None, task_kind: str, cancel: bool) -> bool:
    """True only when the loaded row may be queried or cancelled by this caller.

    A chat caller is bound to ``caller_thread_id``. An HTTP caller passes None
    and is limited to the resource owner. Neither path treats budget exhaustion
    or an already-finished task as a reason to refuse.
    """
    if row is None or row["owner_id"] != owner_id:
        return False
    if caller_thread_id is not None and row["thread_id"] != caller_thread_id:
        return False
    return decide_task_control(
        owner_id=owner_id, caller_thread_id=caller_thread_id,
        resource_owner_id=row["owner_id"], resource_thread_id=row["thread_id"],
        task_id=row["task_id"], task_kind=task_kind, cancel=cancel,
    ) is not None


def register_task_capabilities(registry, runtime):
    def operate(params, context, *, cancel):
        ref = TaskParams.model_validate(params)
        with runtime.db.connection() as connection:
            row = load_task_control(connection, ref.kind, ref.id)
        if not task_is_allowed(
            row, owner_id=context.owner_id, caller_thread_id=context.thread_id,
            task_kind=ref.kind, cancel=cancel,
        ):
            return ToolResult(False, "找不到可操作的任务。", error="TASK_NOT_ACCESSIBLE")
        if ref.kind == "research":
            service = runtime.research
            job = service.cancel(ref.id, owner_id=context.owner_id) if cancel else service.get(ref.id, owner_id=context.owner_id)
            data = {"task_ref": ref.model_dump(), "status": job.status, "phase": job.phase,
                    "title": job.report_title or job.topic, "cancel_requested": bool(job.cancel_requested_at)}
            if job.status in {"COMPLETED", "PARTIAL"}:
                data["report_url"] = f"/api/research/jobs/{job.id}/report"
                data["message_id"] = job.assistant_message_id
        else:
            from .agents import AgentTaskConflict
            service = runtime.agent_tasks
            run = service.get_run(row["task_id"], owner_id=context.owner_id)
            if cancel and run["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                try:
                    run = service.cancel_run(run["id"], "user requested cancellation")
                except AgentTaskConflict:
                    run = service.get_run(run["id"], owner_id=context.owner_id)
            tasks = service.tasks(run["id"], owner_id=context.owner_id)
            data = {"task_ref": ref.model_dump(), "status": run["status"], "title": run["objective"],
                    "cancel_requested": bool(run["cancel_requested_at"]),
                    "progress": [{"role": task["role"], "status": task["status"]} for task in tasks]}
        runtime.conversation.events.append(context.thread_id, context.turn_id,
            "task.cancel_requested" if cancel else "task.queried", "tool",
            {"task_ref": ref.model_dump(), "status": data["status"], "call_id": context.tool_call_id})
        return ToolResult(True, "已读取任务状态。" if not cancel else "已处理取消请求，以返回状态为准。", data=data)

    for name, cancel, description in (
        ("get_task", False, "查询当前对话已创建的研究或专家任务。使用返回过的 task_ref，不得猜测任务 ID。"),
        ("cancel_task", True, "仅在用户明确要求停止任务时取消当前对话的研究或专家任务；已结束任务返回最终状态。"),
    ):
        # Like start_research, this is a scoped task control, not an approval-
        # gated external write. Cancellation must remain immediately available.
        registry.register(ToolSpec(name, description, TaskParams.model_json_schema(), ToolRisk.READ,
            lambda _: ToolResult(False, "需要可信对话上下文。", error="CONTEXT_REQUIRED"),
            context_handler=lambda params, context, cancel=cancel: operate(params, context, cancel=cancel),
            validator=_validator(TaskParams), reject_identity_params=True))
