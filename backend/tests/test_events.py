def test_event_seq_is_contiguous_and_correlation_is_structured(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore

    store = EventStore(Database(tmp_path / "agent.db"))
    first = store.append(
        run_id="run-1",
        goal_id="goal-1",
        event_type="tool.proposed",
        actor="runtime",
        data={"summary": "safe"},
        correlation={"tool_call_id": "call-1", "react_iteration": 1},
    )
    second = store.append(
        run_id="run-1",
        goal_id="goal-1",
        event_type="tool.execution.finished",
        actor="tool",
        data={"ok": True},
        correlation={"tool_call_id": "call-1", "react_iteration": 1},
    )

    assert first.seq == 1
    assert second.seq == 2
    assert first.schema_version == 1
    assert first.correlation["tool_call_id"] == "call-1"
    assert [event.seq for event in store.list("run-1")] == [1, 2]


def test_event_seq_is_per_run(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore

    store = EventStore(Database(tmp_path / "agent.db"))

    store.append("run-1", "goal-1", "run.started", "runtime", {})
    store.append("run-2", "goal-1", "run.started", "runtime", {})

    assert store.list("run-1")[0].seq == 1
    assert store.list("run-2")[0].seq == 1

