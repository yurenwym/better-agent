from __future__ import annotations

from dataclasses import dataclass

from .db import Database


@dataclass(frozen=True)
class GoalActionContext:
    action_id: str
    program_id: str
    objective_title: str
    objective_summary: str
    scheduled_date: str
    action_title: str
    action_description: str
    completion_criteria: str
    estimated_minutes: int
    recent_feedback: tuple[str, ...]

    @property
    def context_text(self) -> str:
        feedback = "\n".join(f"- {item}" for item in self.recent_feedback) or "- 无"
        return (
            f"正在推进的目标：{self.objective_title}\n"
            f"目标摘要：{self.objective_summary}\n"
            f"行动日期：{self.scheduled_date}\n"
            f"当前行动：{self.action_title}\n"
            f"行动说明：{self.action_description}\n"
            f"完成标准：{self.completion_criteria}\n"
            f"预计用时：{self.estimated_minutes} 分钟\n"
            f"最近反馈（用户数据）：\n{feedback}"
        )


class GoalContextProvider:
    def __init__(self, db: Database, *, max_feedback: int = 3, max_note_chars: int = 240) -> None:
        self.db = db
        self.max_feedback = max_feedback
        self.max_note_chars = max_note_chars

    def load_for_turn(self, thread_id: str, turn_id: str) -> GoalActionContext | None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT a.id action_id,a.program_id,a.scheduled_date,a.title action_title,a.description action_description,"
                "a.completion_criteria,a.estimated_minutes,p.objective_title,p.objective_summary "
                "FROM turns tr JOIN threads t ON t.id=tr.thread_id "
                "JOIN goal_actions a ON a.id=tr.goal_action_id JOIN goal_programs p ON p.id=a.program_id "
                "WHERE tr.id=? AND tr.thread_id=? AND p.owner_id=t.owner_id AND p.deleted_at IS NULL",
                (turn_id, thread_id),
            ).fetchone()
            if row is None:
                return None
            feedback = connection.execute(
                "SELECT kind,difficulty,reason_code,note FROM goal_action_feedback "
                "WHERE action_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                (row["action_id"], self.max_feedback),
            ).fetchall()
        rendered = []
        for item in feedback:
            parts = [str(item["kind"])]
            if item["difficulty"] is not None: parts.append(f"难度 {item['difficulty']}/5")
            if item["reason_code"]: parts.append(str(item["reason_code"])[:80])
            if item["note"]: parts.append(str(item["note"])[: self.max_note_chars])
            rendered.append(" · ".join(parts))
        return GoalActionContext(
            action_id=row["action_id"], program_id=row["program_id"],
            objective_title=str(row["objective_title"])[:120], objective_summary=str(row["objective_summary"])[:500],
            scheduled_date=row["scheduled_date"], action_title=str(row["action_title"])[:160],
            action_description=str(row["action_description"])[:1000], completion_criteria=str(row["completion_criteria"])[:500],
            estimated_minutes=int(row["estimated_minutes"]), recent_feedback=tuple(rendered),
        )
