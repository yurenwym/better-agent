from app.db import Database
from app.events import EventStore


def test_runtime_event_append_refreshes_persisted_stats_projection(tmp_path) -> None:
    import asyncio

    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = asyncio.run(runtime.create_goal("Projection", "Persist stats"))

    with runtime.db.connection() as connection:
        row = connection.execute("SELECT stats_json FROM run_stats WHERE run_id = ?", (run.id,)).fetchone()

    assert row is not None


def test_runtime_closes_a_model_attempt_for_each_model_call(tmp_path) -> None:
    import asyncio

    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = asyncio.run(runtime.create_goal("Telemetry", "Count calls"))
    asyncio.run(runtime.handle_message(run.id, "Plan it"))

    events = runtime.events.list(run.id)
    started = [event for event in events if event.type == "model.attempt_started"]
    finished = [event for event in events if event.type == "model.attempt_finished"]

    assert started
    assert len(started) == len(finished)


def test_stats_projector_uses_replacement_usage_and_weighted_stream_metrics(tmp_path) -> None:
    from app.stats import StatsProjector

    store = EventStore(Database(tmp_path / "agent.db"))
    run_id = "run-stats"
    goal_id = "goal-stats"
    at = "2026-08-18T00:00:00+00:00"

    store.append(run_id, goal_id, "interaction.started", "user", {}, occurred_at=at)
    store.append(run_id, goal_id, "plan.version_created", "runtime", {"plan_version_id": "pv-1", "version": 1, "total_steps": 2}, occurred_at=at)
    store.append(run_id, goal_id, "plan.step_finished", "runtime", {"plan_version_id": "pv-1", "plan_step_id": "step-1"}, occurred_at=at)
    store.append(run_id, goal_id, "model.invocation_started", "runtime", {"model_invocation_id": "inv-1"}, occurred_at="2026-08-18T00:00:00.000000+00:00")
    store.append(run_id, goal_id, "model.attempt_started", "runtime", {"model_invocation_id": "inv-1", "model_attempt_id": "att-1"}, occurred_at="2026-08-18T00:00:00.100000+00:00")
    store.append(run_id, goal_id, "model.first_token", "runtime", {"model_attempt_id": "att-1"}, occurred_at="2026-08-18T00:00:00.600000+00:00")
    store.append(run_id, goal_id, "model.usage_updated", "runtime", {"model_attempt_id": "att-1", "usage": {"uncached_input_tokens": 10, "cache_read_tokens": 4, "cache_write_tokens": 2, "output_tokens": 5}}, occurred_at=at)
    store.append(run_id, goal_id, "model.usage_updated", "runtime", {"model_attempt_id": "att-1", "usage": {"uncached_input_tokens": 20, "cache_read_tokens": 4, "cache_write_tokens": 2, "output_tokens": 7}}, occurred_at=at)
    store.append(run_id, goal_id, "model.attempt_finished", "runtime", {"model_attempt_id": "att-1"}, occurred_at="2026-08-18T00:00:01.600000+00:00")
    store.append(run_id, goal_id, "model.invocation_finished", "runtime", {"model_invocation_id": "inv-1", "status": "success"}, occurred_at="2026-08-18T00:00:02.000000+00:00")
    store.append(run_id, goal_id, "tool.execution.started", "tool", {"tool_call_id": "call-1"}, occurred_at="2026-08-18T00:00:03.000000+00:00")
    store.append(run_id, goal_id, "tool.execution.finished", "tool", {"tool_call_id": "call-1"}, occurred_at="2026-08-18T00:00:05.500000+00:00")

    stats = StatsProjector(store.db, store).project(run_id)

    assert stats["interactions"] == 1
    assert stats["plan_completed"] == 1
    assert stats["plan_total"] == 2
    assert stats["model_attempts"] == 1
    assert stats["model_seconds"] == 2.0
    assert stats["tool_calls"] == 1
    assert stats["tool_seconds"] == 2.5
    assert stats["ttft_seconds"] == 0.5
    assert stats["tps"] == 7.0
    assert stats["cache_hit_rate"] == 4 / 26
    assert stats["input_tokens"] == 26
    assert stats["output_tokens"] == 7


def test_stats_projector_marks_missing_spans_and_usage_unavailable(tmp_path) -> None:
    from app.stats import StatsProjector

    store = EventStore(Database(tmp_path / "agent.db"))
    store.append("run-missing", "goal-missing", "model.attempt_started", "runtime", {"model_attempt_id": "att-1"})

    stats = StatsProjector(store.db, store).project("run-missing")

    assert stats["model_attempts"] == 1
    assert stats["ttft_seconds"] is None
    assert stats["tps"] is None
    assert stats["cache_hit_rate"] is None
    assert stats["input_tokens"] is None
    assert stats["output_tokens"] is None


def test_stats_projector_does_not_sum_partial_usage_across_attempts(tmp_path) -> None:
    from app.stats import StatsProjector

    store = EventStore(Database(tmp_path / "agent.db"))
    store.append("run-partial", "goal-partial", "model.attempt_started", "runtime", {"model_attempt_id": "att-1"})
    store.append("run-partial", "goal-partial", "model.usage_updated", "runtime", {"model_attempt_id": "att-1", "usage": {"uncached_input_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 1}})
    store.append("run-partial", "goal-partial", "model.attempt_finished", "runtime", {"model_attempt_id": "att-1"})
    store.append("run-partial", "goal-partial", "model.attempt_started", "runtime", {"model_attempt_id": "att-2"})
    store.append("run-partial", "goal-partial", "model.attempt_finished", "runtime", {"model_attempt_id": "att-2"})

    stats = StatsProjector(store.db, store).project("run-partial")

    assert stats["input_tokens"] is None
    assert stats["output_tokens"] is None
