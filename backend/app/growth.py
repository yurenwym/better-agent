from __future__ import annotations

from typing import Any

OWNER_ID = "local-user"


class GrowthProfileService:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.db = runtime.db

    def profile(self, owner_id: str = OWNER_ID) -> dict[str, Any]:
        with self.db.connection() as connection:
            programs = connection.execute(
                "SELECT * FROM goal_programs WHERE owner_id=? AND deleted_at IS NULL ORDER BY updated_at DESC,id",
                (owner_id,),
            ).fetchall()
            feedback = connection.execute(
                "SELECT f.actual_minutes,f.difficulty,f.sensitivity FROM goal_action_feedback f "
                "JOIN goal_actions a ON a.id=f.action_id JOIN goal_programs p ON p.id=a.program_id "
                "WHERE f.owner_id=? AND p.owner_id=? ORDER BY f.created_at,f.id",
                (owner_id, owner_id),
            ).fetchall()
            action_counts = connection.execute(
                "SELECT SUM(a.status='COMPLETED') completed,SUM(a.status='SKIPPED') skipped,SUM(a.status='DEFERRED') deferred "
                "FROM goal_actions a JOIN goal_programs p ON p.id=a.program_id WHERE p.owner_id=? AND p.deleted_at IS NULL",
                (owner_id,),
            ).fetchone()
            adjustments = connection.execute(
                "SELECT COUNT(*) total FROM goal_adjustment_proposals WHERE owner_id=? AND status='ACCEPTED'",
                (owner_id,),
            ).fetchone()["total"]
        history = [self._program(item, owner_id) for item in programs]
        completed = [item for item in programs if item["status"] == "COMPLETED"]
        difficulties = [int(item["difficulty"]) for item in feedback if item["difficulty"] is not None]
        durations = [int(item["actual_minutes"]) for item in feedback if item["actual_minutes"] is not None]
        return {
            "owner_id": owner_id,
            "metrics": {
                "total_programs": len(programs),
                "completed_programs": len(completed),
                "completed_actions": int(action_counts["completed"] or 0),
                "skipped_actions": int(action_counts["skipped"] or 0),
                "deferred_actions": int(action_counts["deferred"] or 0),
                "average_difficulty": round(sum(difficulties) / len(difficulties), 2) if difficulties else None,
                "average_actual_minutes": round(sum(durations) / len(durations), 2) if durations else None,
                "accepted_adjustments": int(adjustments),
            },
            "programs": history,
        }

    def program(self, program_id: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        try:
            value = self.runtime.goal_programs.get(program_id, owner_id)
        except KeyError:
            raise
        return {"program": self._program_value(value, owner_id), "profile": self.profile(owner_id)}

    def _program(self, row, owner_id: str) -> dict[str, Any]:
        return self._program_value(self.runtime.goal_programs.get(row["id"], owner_id), owner_id)

    def _program_value(self, value: dict[str, Any], owner_id: str) -> dict[str, Any]:
        return {
            "id": value["id"], "objective_title": value["objective_title"], "objective_summary": value["objective_summary"],
            "status": value["status"], "start_date": value["start_date"], "end_date": value["end_date"],
            "version": value["version"], "progress": value["progress"], "completion_summary": value.get("completion_summary"),
            "completion_episode_id": value.get("completion_episode_id"), "source_plan_document_id": value["source_plan_document_id"],
        }
