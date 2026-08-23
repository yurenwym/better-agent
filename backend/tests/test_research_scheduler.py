from __future__ import annotations

from datetime import datetime, timezone

from app.conversation import ConversationService
from app.db import Database
from app.research.scheduler import ScheduleService, next_run
from app.research.service import ResearchService


def test_next_run_uses_iana_timezone_across_dst() -> None:
    before = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
    result = next_run("daily", "03:30", None, None, "America/New_York", before)
    assert result.tzinfo == timezone.utc
    assert result.isoformat() == "2026-03-08T07:30:00+00:00"


def test_schedule_due_occurrence_is_idempotent_and_missed_runs_once(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    conversation = ConversationService(db)
    research = ResearchService(db, conversation.events)
    schedules = ScheduleService(db, conversation, research)
    schedule = schedules.create(name="日报", topic="AI 新闻", source_scopes=("web",), trigger_type="interval_hours", interval_hours=24, timezone_name="Asia/Shanghai", enabled=True)
    with db.transaction() as connection:
        connection.execute("UPDATE research_schedules SET next_run_at='2026-01-01T00:00:00+00:00' WHERE id=?", (schedule.id,))
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    first = schedules.tick(now)
    second = schedules.tick(now)
    assert len(first) == 1 and second == []
    assert len(research.list(schedule_id=schedule.id)) == 1
    assert schedules.get(schedule.id).next_run_at > now.isoformat()


def test_run_now_idempotency_and_soft_delete_preserves_jobs(tmp_path) -> None:
    db = Database(tmp_path / "agent.db"); conversation = ConversationService(db); research = ResearchService(db, conversation.events)
    schedules = ScheduleService(db, conversation, research)
    schedule = schedules.create(name="周报", topic="骑行进展", source_scopes=("web",), trigger_type="weekly", trigger_time="09:00", trigger_weekday=1, timezone_name="Asia/Shanghai", enabled=False)
    one = schedules.run_now(schedule.id, "same")
    two = schedules.run_now(schedule.id, "same")
    assert one.id == two.id
    schedules.delete(schedule.id)
    assert schedules.list() == []
    assert research.get(one.id).schedule_id == schedule.id

