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
        "plan_documents",
        "plan_document_versions",
        "plan_write_intents",
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


def test_database_migrates_deleted_at_on_an_existing_plan_document_table(tmp_path) -> None:
    from app.db import Database

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE plan_documents ("
            "id TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, "
            "current_version_id TEXT, projected_version_id TEXT, file_status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    Database(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(plan_documents)")}
    assert "deleted_at" in columns


def test_database_migrates_deleted_at_on_an_existing_thread_table(tmp_path) -> None:
    from app.db import Database

    path = tmp_path / "legacy-thread.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, "
            "active_turn_id TEXT, next_event_seq INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    Database(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
    assert "deleted_at" in columns


def test_events_are_append_only(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore

    db = Database(tmp_path / "agent.db")
    store = EventStore(db)
    store.append("run-1", "goal-1", "run.started", "runtime", {})

    with db.connection() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE events SET data_json = '{}' WHERE run_id = 'run-1'")

