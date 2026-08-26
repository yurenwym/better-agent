from __future__ import annotations

import asyncio
import json

import pytest

from app.experience_observer import ManagedExperienceObserver
from app.startup import build_runtime


def test_observer_projects_terminal_run_once_without_sensitive_text(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    run = asyncio.run(runtime.create_goal("完成骑行计划", "建立每周习惯"))
    event = runtime.events.append(
        run.id,
        run.goal_id,
        "run.failed",
        "runtime",
        {"reason": "tool failed", "note": "private health detail", "retry_count": 2},
    )

    first = runtime.observer.observe()
    second = runtime.observer.observe()

    assert first["created"] == 1
    assert second["created"] == 0
    with runtime.db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM evolution_experiences WHERE source_kind='run' AND source_id=?",
            (run.id,),
        ).fetchone()
    assert row is not None
    assert row["signal_type"] == "run_failed"
    assert row["severity"] == "error"
    assert row["source_event_id"] == event.event_id
    assert "private health detail" not in json.dumps(dict(row), ensure_ascii=False)
    assert runtime.evolution.list_experiences()[0]["evidence"]["retry_count"] == 2


def test_observer_collapses_retry_events_and_keeps_owner_scope(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    local = asyncio.run(runtime.create_goal("本地目标", "目标"))
    first = runtime.events.append(local.id, local.goal_id, "run.retrying", "runtime", {"attempt": 1})
    terminal = runtime.events.append(local.id, local.goal_id, "run.cancelled", "user", {"reason": "user stopped"})

    other = asyncio.run(runtime.create_goal("另一个目标", "目标"))
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE goals SET project_id=? WHERE id=?", ("other-owner", other.goal_id))
    runtime.events.append(other.id, other.goal_id, "run.failed", "runtime", {"reason": "other"})

    result = runtime.observer.observe(owner_id="local-user")

    assert result["created"] == 1
    assert result["skipped_non_terminal"] >= 1
    records = runtime.evolution.list_experiences(owner_id="local-user")
    assert len(records) == 1
    assert records[0]["source_event_id"] == terminal.event_id
    assert records[0]["source_event_id"] == terminal.event_id


def test_observer_uses_stable_failure_tags_for_agent_and_goal_sources(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    run = asyncio.run(runtime.create_goal("目标", "目标"))
    runtime.events.append(run.id, run.goal_id, "budget.exhausted", "runtime", {"reason": "budget"})
    runtime.events.append(run.id, run.goal_id, "run.blocked", "runtime", {"reason": "budget"})
    runtime.events.append(run.id, run.goal_id, "run.failed", "runtime", {"reason": "budget"})

    result = runtime.observer.observe()

    assert result["created"] == 1
    record = runtime.evolution.list_experiences()[0]
    assert record["failure_tags"] == ["budget_exhausted", "run_failed"]
    assert record["dataset_partition"] == "DISCOVERY"


def test_observer_keeps_action_review_and_adjustment_terminal_lineages_independent(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    thread = runtime.conversation.create_thread("observer-goal")
    version = runtime.plan_documents.save_model_revision(
        thread_id=thread.id, title="计划", markdown_content="# 计划", source_turn_id=None,
        source_message_id=None, actor="user",
    )
    with runtime.db.transaction() as connection:
        now = "2026-08-26T00:00:00+00:00"
        connection.execute(
            "INSERT INTO goal_programs(id,owner_id,source_thread_id,source_plan_document_id,source_plan_document_version_id,source_plan_content_hash,status,compile_status,timezone,start_date,end_date,daily_minutes,created_at,updated_at) VALUES ('p','local-user',?,?,?,?,'COMPLETED','READY','UTC','2026-08-26','2026-08-26',30,?,?)",
            (thread.id, version.plan_document_id, version.id, version.content_hash, now, now),
        )
        connection.execute(
            "INSERT INTO goal_program_versions(id,program_id,version,source_plan_document_version_id,structure_json,change_summary,actor,created_at) VALUES ('pv','p',1,?,'{}','','test',?)",
            (version.id, now),
        )
        connection.execute(
            "INSERT INTO goal_actions(id,program_id,program_version_id,logical_key,scheduled_date,position,title,description,estimated_minutes,completion_criteria,required,status,created_at,updated_at) VALUES ('a1','p','pv','day-1','2026-08-26',1,'行动','',30,'完成',1,'COMPLETED',?,?)",
            (now, now),
        )
        for seq, event_type, action_id, data in [
            (1, "action.completed", "a1", {}),
            (2, "review.completed", None, {"review_id": "r1"}),
            (3, "adjustment.rejected", None, {"proposal_id": "x1"}),
        ]:
            connection.execute(
                "INSERT INTO goal_program_events(event_id,program_id,seq,action_id,type,actor,occurred_at,data_json) VALUES (?,?,?,?,?,'test',?,?)",
                (f"e{seq}", "p", seq, action_id, event_type, now, json.dumps(data)),
            )

    assert runtime.observer.observe()["created"] == 3
    assert {item["source_kind"] for item in runtime.evolution.list_experiences()} == {"action", "review", "adjustment"}


@pytest.mark.asyncio
async def test_managed_observer_automatically_projects_new_terminal_events(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    worker = ManagedExperienceObserver(runtime.observer, poll_interval=.01)
    await worker.start()
    try:
        run = await runtime.create_goal("自动采集", "验证终态事件")
        runtime.events.append(run.id, run.goal_id, "run.completed", "runtime", {"answer": "sensitive"})
        for _ in range(100):
            if runtime.evolution.list_experiences():
                break
            await asyncio.sleep(.01)
        assert len(runtime.evolution.list_experiences()) == 1
    finally:
        await worker.stop()

    await worker.start()
    await asyncio.sleep(.03)
    await worker.stop()
    assert len(runtime.evolution.list_experiences()) == 1
