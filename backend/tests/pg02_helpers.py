"""Small PostgreSQL helpers shared by the PG02 batch-2 modules.

These replace SQLite-only test helpers (``test_runtime.make_runtime`` and
``test_static_archive_policy._thread_with_turns``) without migrating those
modules. The database is always the caller's PostgreSQL fixture.
"""
from __future__ import annotations

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.memory_archive import ConversationArchiver
from app.memory_v2 import MemoryStore


@pytest.fixture()
def database(migrated_postgres_url, isolated_postgres_test, tmp_path, monkeypatch):
    """PostgreSQL-backed database with a per-test empty schema."""
    monkeypatch.delenv("BETTER_AGENT_TEST_ALLOW_SQLITE", raising=False)
    monkeypatch.setenv("DATABASE_URL", str(migrated_postgres_url))
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    assert db.backend == "postgresql"
    yield db
    db.close()


def make_runtime(database, tmp_path, model):
    """The ``test_runtime.make_runtime`` construction against a provided PG DB."""
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.memory import MemoryService
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry

    events = EventStore(database)
    approvals = ApprovalService(database)
    return AgentRuntime(
        db=database,
        events=events,
        plans=PlanVersionService(database),
        approvals=approvals,
        checkpoints=CheckpointStore(database),
        memory=MemoryService(database, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "workspace", db=database, approval_service=approvals),
        model=model,
    )


def thread_with_turns(database, tmp_path, sizes, *, owner_id="owner-a"):
    """The ``_thread_with_turns`` fixture against a provided PG DB."""
    thread = ConversationService(database).create_thread(owner_id=owner_id)
    now = "2026-01-01T00:00:00+00:00"
    with database.transaction() as connection:
        for index, size in enumerate(sizes):
            turn_id = f"turn-{index}"
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED',?,?)",
                (turn_id, thread.id, turn_id, now, now),
            )
            connection.execute(
                "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                "content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                (f"msg-{index}", thread.id, turn_id, "user", "u" * size, size, index * 2 + 1, now, now),
            )
            connection.execute(
                "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                "content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                (f"msg-{index}-a", thread.id, turn_id, "assistant", "a" * size, size, index * 2 + 2, now, now),
            )
    archiver = ConversationArchiver(database, MemoryStore(database, tmp_path / "memory"), None)
    return database, thread, archiver


def completed_thread(database, *, owner_id="owner-a"):
    """The ``_completed_thread`` fault fixture against a provided PG DB."""
    conversation = ConversationService(database)
    thread = conversation.create_thread(owner_id=owner_id)
    now = "2026-01-01T00:00:00+00:00"
    with database.transaction() as connection:
        turn = "fault-turn"
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
            "VALUES (?,?,?,'COMPLETED',?,?)",
            (turn, thread.id, turn, now, now),
        )
        connection.execute(
            "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
            "content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
            ("fault-user", thread.id, turn, "user", "source", 6, 1, now, now),
        )
    return database, thread
