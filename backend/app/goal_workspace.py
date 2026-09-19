from __future__ import annotations

from datetime import date
from typing import Any


OWNER_ID = "local-user"


class GoalWorkspaceService:
    """Read-only projection of existing goal facts.

    The workspace deliberately has no table or mutable status of its own.  A refresh
    re-derives the same view from the plan, program, review and memory records.
    """

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.db = runtime.db

    def get(self, resource_id: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        plan = self._plan(resource_id, owner_id)
        thread_id = plan.thread_id
        programs = [
            item for item in self.runtime.goal_programs.list(owner_id)
            if item["source_plan_document_id"] == plan.id
        ]
        program = programs[0] if programs else None
        current = self._current_review(program, owner_id) if program else None
        phase, next_action = self._phase(plan, program, current)
        research = self._research(thread_id)
        episodes = self._episodes(thread_id, owner_id)
        version = self.runtime.plan_documents.current_version(plan.id)
        return {
            "resource_id": resource_id,
            "thread_id": thread_id,
            "plan_document_id": plan.id,
            "phase": phase,
            "next_action": next_action,
            "sources": self._sources(plan, version, research, program, current, episodes),
            "plan": {
                "id": plan.id,
                "title": version.title,
                "version": version.version,
                "file_status": plan.file_status,
            },
            "program": self._program_summary(program),
            "today": self._today(program),
            "review": current,
            "research": research,
            "growth": self._growth(episodes, program),
        }

    def _plan(self, resource_id: str, owner_id: str):
        try:
            plan = self.runtime.plan_documents.get_document(resource_id)
        except KeyError:
            plan = None
        if plan is None:
            with self.db.connection() as connection:
                row = connection.execute(
                    "SELECT d.id FROM plan_documents d JOIN threads t ON t.id=d.thread_id "
                    "WHERE t.id=? AND t.owner_id=? AND d.deleted_at IS NULL",
                    (resource_id, owner_id),
                ).fetchone()
            if row is None:
                raise KeyError(resource_id)
            plan = self.runtime.plan_documents.get_document(row["id"])
        with self.db.connection() as connection:
            owned = connection.execute(
                "SELECT 1 FROM threads WHERE id=? AND owner_id=? AND deleted_at IS NULL",
                (plan.thread_id, owner_id),
            ).fetchone()
        if owned is None:
            raise KeyError(resource_id)
        return plan

    def _phase(self, plan, program: dict[str, Any] | None, review: dict[str, Any] | None):
        if program is None:
            return "PLANNING", self._action("preview_program", "生成执行预览", f"/plans/{plan.id}", plan.id, "计划已保存，下一步是生成可执行的行动安排。")
        status = program["status"]
        if status == "DRAFT":
            if program.get("compile_status") == "READY":
                return "READY_TO_START", self._action("activate_program", "开始执行计划", f"/plans/{plan.id}", program["id"], "执行预览已准备好，确认后会生成今日行动清单。")
            return "PLANNING", self._action("retry_compile", "重试生成执行安排", f"/plans/{plan.id}", program["id"], "执行安排尚未准备好。")
        if status == "ACTIVE":
            if review and review.get("proposal") and review["proposal"].get("status") == "PENDING":
                return "ADJUSTING", self._action("accept_adjustment", "查看并确认计划调整", "/today", review["proposal"]["id"], "复盘提出了计划调整，等待你的确认。")
            if review and review.get("status") in {"QUEUED", "RUNNING"}:
                return "REVIEWING", self._action("open_review", "查看每日复盘", "/today", program["id"], "今日记录已提交，Agent 正在生成复盘。")
            action = self._next_scheduled(program)
            if action:
                return "EXECUTING", self._action("complete_action", "完成下一项行动", "/today", action["id"], "今日行动中有待完成行动。")
            return "EXECUTING", self._action("open_today", "查看今日行动", "/today", program["id"], "查看当前执行进度和完整日程。")
        if status == "PAUSED":
            return "PAUSED", self._action("resume_program", "恢复执行", "/today", program["id"], "目标已暂停，恢复后会继续显示行动。")
        if status == "COMPLETED":
            return "COMPLETED", None
        return "CANCELLED", None

    def _current_review(self, program: dict[str, Any], owner_id: str) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT local_date FROM goal_daily_reviews WHERE program_id=? AND owner_id=? ORDER BY local_date DESC,created_at DESC",
                (program["id"], owner_id),
            ).fetchall()
        for row in rows:
            review = self.runtime.goal_reviews.for_program_date(program["id"], row["local_date"], owner_id)
            if review and (review["status"] in {"QUEUED", "RUNNING"} or review.get("proposal")):
                return review
        return None

    def _next_scheduled(self, program: dict[str, Any]) -> dict[str, Any] | None:
        actions = [item for item in program["actions"] if item["status"] == "SCHEDULED"]
        return min(actions, key=lambda item: (item["scheduled_date"], item["position"], item["id"]), default=None)

    def _today(self, program: dict[str, Any] | None) -> dict[str, Any] | None:
        if program is None:
            return None
        local_date = date.today().isoformat()
        actions = [item for item in program["actions"] if item["scheduled_date"] == local_date and item["status"] == "SCHEDULED"]
        return {"program_id": program["id"], "date": local_date, "action_count": len(actions), "estimated_minutes": sum(item["estimated_minutes"] for item in actions)}

    def _research(self, thread_id: str) -> list[dict[str, Any]]:
        service = getattr(self.runtime, "research", None)
        if service is None:
            return []
        return [{"id": job.id, "topic": job.topic, "status": job.status, "phase": job.phase} for job in service.list(thread_id=thread_id, limit=10)]

    def _episodes(self, thread_id: str, owner_id: str) -> list[dict[str, Any]]:
        store = getattr(self.runtime, "memory_store", None)
        if store is not None:
            return [{"id": item.id, "summary": item.summary, "created_at": item.created_at} for item in store.list_episodes(owner_id, thread_id)]
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT id,summary,created_at FROM memory_episodes WHERE owner_id=? AND thread_id=? AND status='ACTIVE' ORDER BY created_at DESC,id",
                (owner_id, thread_id),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _program_summary(program: dict[str, Any] | None) -> dict[str, Any] | None:
        if program is None:
            return None
        return {key: program[key] for key in ("id", "objective_title", "status", "start_date", "end_date", "version")}

    @staticmethod
    def _growth(episodes: list[dict[str, Any]], program: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "episode_id": program.get("completion_episode_id") if program and program.get("completion_episode_id") else None,
            "episode_count": len(episodes),
            "latest_summary": episodes[0]["summary"] if episodes else None,
        }

    @staticmethod
    def _sources(plan, version, research, program, review, episodes) -> list[dict[str, Any]]:
        sources = [{"kind": "plan", "id": plan.id, "label": f"计划 v{version.version}"}]
        if research:
            sources.append({"kind": "research", "id": research[0]["id"], "label": "研究结果"})
        if program:
            sources.append({"kind": "program", "id": program["id"], "label": "执行计划"})
        if review:
            sources.append({"kind": "review", "id": review["id"], "label": "每日复盘"})
        if episodes:
            sources.append({"kind": "memory", "id": episodes[0]["id"], "label": "成长经历"})
        return sources

    @staticmethod
    def _action(kind: str, label: str, href: str, resource_id: str, reason: str) -> dict[str, str]:
        return {"kind": kind, "label": label, "href": href, "resource_id": resource_id, "reason": reason}
