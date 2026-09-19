from __future__ import annotations

from dataclasses import dataclass
import json
from datetime import datetime

from .db import Database
from .goal_programs import GoalProgramService, _timezone, program_day_time


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
    time_context: str = ""

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
            f"{self.time_context}\n"
            "用户正在求助。先回应具体阻塞，给一个能马上执行的下一步；不要主动展开多专家讨论或长篇规划。\n"
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
            program = connection.execute("SELECT * FROM goal_programs WHERE id=?", (row["program_id"],)).fetchone()
            actual_date = datetime.now(_timezone(program["timezone"])).date().isoformat()
            timing = program_day_time(connection, program, actual_date)
            effective = GoalProgramService.latest_feedback(connection, row["action_id"])
            effective["actual_minutes"] = GoalProgramService.latest_feedback(connection, row["action_id"], actual_date)["actual_minutes"]
            remaining = "未知（记录不完整，先确认可用时间）" if timing["remaining_minutes"] is None else str(timing["remaining_minutes"])+" 分钟"
            time_context = f"实际日期：{actual_date}；本目标当日已记录投入：{timing['spent_minutes']} 分钟；本目标剩余预算：{remaining}。不代表所有目标的个人总预算。"
            feedback = [{"kind": "effective", **effective, "details_json": json.dumps({key: effective[key] for key in ("completed_work", "remaining_work", "output") if effective.get(key) is not None})}]
        rendered = []
        for item in feedback:
            parts = [str(item["kind"])]
            if item["difficulty"] is not None: parts.append(f"难度 {item['difficulty']}/5")
            if item["actual_minutes"] is not None: parts.append(f"实际用时 {item['actual_minutes']} 分钟")
            if item["reason_code"]: parts.append(str(item["reason_code"])[:80])
            if item["note"]: parts.append(str(item["note"])[: self.max_note_chars])
            for key,value in json.loads(item["details_json"]).items():
                parts.append(f"{key}: {str(value)[:self.max_note_chars]}")
            rendered.append(" · ".join(parts))
        return GoalActionContext(
            action_id=row["action_id"], program_id=row["program_id"],
            objective_title=str(row["objective_title"])[:120], objective_summary=str(row["objective_summary"])[:500],
            scheduled_date=row["scheduled_date"], action_title=str(row["action_title"])[:160],
            action_description=str(row["action_description"])[:1000], completion_criteria=str(row["completion_criteria"])[:500],
            estimated_minutes=int(row["estimated_minutes"]), recent_feedback=tuple(rendered),
            time_context=time_context,
        )
