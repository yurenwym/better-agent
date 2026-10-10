"""Conversation controls for existing research and expert tasks."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .conversation_capabilities import _validator
from .policy_engine import PolicyAction, PolicyInput, decide
from .tool_contracts import ToolResult, ToolRisk, ToolSpec


class TaskParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["research", "expert"]
    id: str = Field(min_length=1, max_length=200)


def register_task_capabilities(registry, runtime):
    def operate(params, context, *, cancel):
        ref = TaskParams.model_validate(params)
        with runtime.db.connection() as connection:
            if ref.kind == "research":
                row = connection.execute(
                    "SELECT j.id,j.thread_id,h.owner_id FROM research_jobs j "
                    "JOIN threads h ON h.id=j.thread_id WHERE j.id=? AND h.deleted_at IS NULL", (ref.id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT r.id,r.thread_id,r.owner_id FROM agent_tasks t JOIN agent_runs r ON r.id=t.agent_run_id "
                    "JOIN threads h ON h.id=r.thread_id AND h.owner_id=r.owner_id "
                    "WHERE t.id=? AND r.coordinator_task_id=t.id AND h.deleted_at IS NULL", (ref.id,),
                ).fetchone()
        authorized = row is not None and row["owner_id"] == context.owner_id and row["thread_id"] == context.thread_id
        decision = decide(PolicyInput(registered=True, allowed=authorized, identity_valid=bool(context.owner_id)))
        if decision.action == PolicyAction.DENY:
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
            run = service.get_run(row["id"], owner_id=context.owner_id)
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
