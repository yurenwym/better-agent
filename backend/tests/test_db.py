import sqlite3

import pytest


def test_database_initializes_required_tables_and_wal(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")

    with db.connection() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert journal_mode.lower() == "wal"
    assert {
        "goals",
        "sessions",
        "runs",
        "interactions",
        "plan_versions",
        "plan_steps",
        "checkpoints",
        "messages",
        "events",
        "approvals",
        "memory_candidates",
        "memory_file_versions",
        "run_stats",
    } <= tables


def test_database_transaction_rolls_back_on_error(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")

    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO goals(id, title, description, created_at, updated_at) "
                "VALUES ('g1', 'Goal', 'Description', 'now', 'now')"
            )
            raise RuntimeError("abort")

    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0


def test_events_are_append_only(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore

    db = Database(tmp_path / "agent.db")
    store = EventStore(db)
    store.append("run-1", "goal-1", "run.started", "runtime", {})

    with db.connection() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE events SET data_json = '{}' WHERE run_id = 'run-1'")

