from app.db import Database


def test_conversation_schema_is_durable_and_migrates_source_turn_id(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")

    with db.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"threads", "turns", "turn_jobs", "thread_messages", "thread_events"} <= tables
        run_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "source_turn_id" in run_columns


def test_conversation_transaction_rolls_back_all_rows(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")

    try:
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO threads(id, title, version, active_turn_id, next_event_seq, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("thread-1", "Test", 0, None, 1, "now", "now"),
            )
            connection.execute(
                "INSERT INTO turns(id, thread_id, client_turn_id, status, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("turn-1", "thread-1", "client-1", "ACCEPTED", 0, "now", "now"),
            )
            raise RuntimeError("rollback")
    except RuntimeError:
        pass

    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_thread_event_store_uses_persisted_thread_cursor(tmp_path) -> None:
    from app.events import ThreadEventStore

    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id, title, version, next_event_seq, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("thread-1", "Test", 0, 1, "now", "now"),
        )

    events = ThreadEventStore(db)
    first = events.append("thread-1", "turn-1", "turn.accepted", "user", {})
    second = events.append("thread-1", "turn-1", "turn.started", "worker", {})

    assert [event.seq for event in events.list("thread-1")] == [1, 2]
    assert first.event_id != second.event_id
