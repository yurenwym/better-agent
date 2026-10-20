"""PG02: the PostgreSQL turn-completion archive signal trigger."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import pg_fixtures
from app.conversation import ConversationService
from pg02_helpers import database

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _upgrade(database_url: str, revision: str) -> None:
    pg_fixtures.require_test_database(database_url)
    result = subprocess.run(
        [
            sys.executable, "-m", "alembic",
            "-c", str(BACKEND_ROOT / "alembic.ini"), "upgrade", revision,
        ],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic upgrade {revision} failed (exit {result.returncode}):\n"
        + result.stderr[-4000:]
    )


def _turn(database, thread_id: str, turn_id: str, status: str = "ROUTING") -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (turn_id, thread_id, turn_id, status, "n", "n"),
        )


def _signal_count(database, turn_id: str) -> int:
    with database.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM memory_archive_signals WHERE turn_id=?", (turn_id,)
        ).fetchone()[0]


def test_completion_transition_creates_exactly_one_signal(database):
    thread = ConversationService(database).create_thread(owner_id="owner-a")
    _turn(database, thread.id, "signal-turn")
    assert _signal_count(database, "signal-turn") == 0

    with database.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id='signal-turn'")
    assert _signal_count(database, "signal-turn") == 1

    # A repeated COMPLETED update is a no-op and must not duplicate the signal.
    with database.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id='signal-turn'")
    assert _signal_count(database, "signal-turn") == 1


def test_nonterminal_update_creates_no_signal(database):
    thread = ConversationService(database).create_thread(owner_id="owner-a")
    _turn(database, thread.id, "running-turn")
    with database.transaction() as connection:
        connection.execute("UPDATE turns SET status='RUNNING' WHERE id='running-turn'")
    assert _signal_count(database, "running-turn") == 0


def test_rollback_of_terminal_update_rolls_back_the_signal(database):
    thread = ConversationService(database).create_thread(owner_id="owner-a")
    _turn(database, thread.id, "rollback-turn")
    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            connection.execute("UPDATE turns SET status='COMPLETED' WHERE id='rollback-turn'")
            raise RuntimeError("rollback the terminal update")
    assert _signal_count(database, "rollback-turn") == 0
    with database.connection() as connection:
        status = connection.execute(
            "SELECT status FROM turns WHERE id='rollback-turn'"
        ).fetchone()[0]
    assert status == "ROUTING"


def test_repeated_transition_reuses_the_signal_row(database):
    thread = ConversationService(database).create_thread(owner_id="owner-a")
    _turn(database, thread.id, "conflict-turn")
    with database.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id='conflict-turn'")
    with database.connection() as connection:
        created = connection.execute(
            "SELECT created_at FROM memory_archive_signals WHERE turn_id='conflict-turn'"
        ).fetchone()[0]
    assert created

    # Completed -> running -> completed fires the trigger again; ON CONFLICT
    # must keep the original signal row unchanged.
    with database.transaction() as connection:
        connection.execute("UPDATE turns SET status='RUNNING' WHERE id='conflict-turn'")
    with database.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id='conflict-turn'")
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT created_at FROM memory_archive_signals WHERE turn_id='conflict-turn'"
        ).fetchall()
    assert len(rows) == 1 and rows[0][0] == created


def test_inserting_completed_turn_does_not_signal(database):
    thread = ConversationService(database).create_thread(owner_id="owner-a")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
            "VALUES ('inserted-complete',?,'inserted-complete','COMPLETED','n','n')",
            (thread.id,),
        )
    assert _signal_count(database, "inserted-complete") == 0


def test_old_0033_to_new_head_creates_the_signal(postgres_url):
    owned = pg_fixtures.allocate_owned_database(str(postgres_url))
    try:
        pg_fixtures.create_owned_database(owned)
        _upgrade(owned.url, "20261019_0033")
        with pg_fixtures._connect(owned.url, autocommit=True) as connection:
            connection.execute(
                "INSERT INTO threads(id,title,owner_id,created_at,updated_at) "
                "VALUES ('thr','t','owner-a','n','n')"
            )
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES ('turn','thr','turn','ROUTING','n','n')"
            )
        _upgrade(owned.url, "head")
        with pg_fixtures._connect(owned.url, autocommit=True) as connection:
            connection.execute("UPDATE turns SET status='COMPLETED' WHERE id='turn'")
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_archive_signals WHERE turn_id='turn'"
            ).fetchone()[0]
        assert count == 1
    finally:
        pg_fixtures.drop_owned_database(owned)
