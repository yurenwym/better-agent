from __future__ import annotations

import asyncio
import json

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

    result = runtime.observer.observe()

    assert result["created"] == 1
    record = runtime.evolution.list_experiences()[0]
    assert record["failure_tags"] == ["budget_exhausted"]
    assert record["dataset_partition"] == "DISCOVERY"
