"""P01-P04: the execution context on the real PostgreSQL schema.

These cases exercise the parts SQLite cannot: the Alembic upgrade path (empty
database *and* an existing database at the previous head), ``task_budget_roots``,
and real concurrent writers on the same row.

Everything here goes through the isolated-database guard: ``migrated_postgres_url``
already refuses a non-test target before alembic runs, and the extra database
created for the upgrade-path case is checked the same way before it is migrated.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.db import POSTGRES_SCHEMA_HEAD, Database
from app.execution_context import (
    HarnessContextError,
    create_child_context,
    create_root_context,
    execution_context_digest,
    serialize_context,
)
from app.harness_context_store import ContextStoreConflict, HarnessContextStore
from db_target_guard import assert_isolated_test_database

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "20260921_0023"
CONTEXT_TABLES = ("turns", "turn_tool_calls", "model_invocations")
CONTEXT_COLUMNS = ("execution_context_json", "execution_context_digest")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _alembic(database_url: str, revision: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_ROOT / "alembic.ini"), "upgrade", revision],
        cwd=BACKEND_ROOT, env=environment, check=False, capture_output=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, result.stderr[-4000:]


def _sibling_url(base_url: str, suffix: str) -> str:
    parsed = urlsplit(base_url)
    name = parsed.path.lstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{name}{suffix}", parsed.query, ""))


def _admin_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/postgres", "", ""))


@pytest.fixture
def fresh_database(migrated_postgres_url):
    """A second, independently migrated database for the upgrade-path cases."""
    url = _sibling_url(str(migrated_postgres_url), f"_{uuid.uuid4().hex[:8]}_test")
    assert_isolated_test_database(url)
    name = urlsplit(url).path.lstrip("/")
    with psycopg.connect(_admin_url(str(migrated_postgres_url)), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield url
    finally:
        with psycopg.connect(_admin_url(str(migrated_postgres_url)), autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _columns(url: str, table: str) -> set[str]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        return {
            row["column_name"] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=%s", (table,),
            )
        }


def _seed(db: Database, *, owner_id: str = "local-user"):
    """A thread, a turn, a tool call and a model invocation on the real schema."""
    from app.chat_tools import ChatToolCallStore
    from app.conversation import ConversationService

    conversation = ConversationService(db)
    thread = conversation.create_thread("PG 上下文", owner_id=owner_id)
    accepted = conversation.accept_turn(
        thread.id, f"pg-{uuid.uuid4().hex[:8]}", "生成执行预览", [], owner_id=owner_id,
    )
    store = ChatToolCallStore(db)
    call = store.create(
        turn_id=accepted.turn_id, thread_id=thread.id, tool_name="get_today_tasks",
        params={}, risk="READ", call_id=f"chat-tool-{uuid.uuid4().hex}",
    )
    invocation_id = f"model_invocation_{uuid.uuid4().hex}"
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_invocations(id,owner_id,run_id,thread_id,turn_id,agent_task_id,role,purpose,"
            "runtime_bundle_id,routing_policy_id,routing_policy_digest,route_snapshot_json,request_digest,"
            "tool_schema_digest,context_snapshot_digest,status,idempotency_key,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                invocation_id, owner_id, f"chat-turn:{accepted.turn_id}", thread.id, accepted.turn_id, None,
                "conversation", "route_and_respond", None, None, "direct", "{}", "req", "tools", "snap",
                "SUCCEEDED", f"idem-{uuid.uuid4().hex}", "2026-09-30T00:00:00+00:00",
            ),
        )
    return thread, accepted, call, invocation_id


def _context_for(db: Database, turn: str, **overrides):
    """A root context bound to ``turn``, with any field overridden on purpose.

    ``turn`` is deliberately not named ``turn_id`` so that a caller can override
    ``turn_id`` to a conflicting value without hitting a duplicate argument.
    """
    resolved = overrides.pop("turn_id", turn)
    with db.connection() as connection:
        row = connection.execute("SELECT * FROM turns WHERE id=?", (turn,)).fetchone()
        thread = connection.execute(
            "SELECT owner_id,project_id FROM threads WHERE id=?", (row["thread_id"],),
        ).fetchone()
    fields = {
        "owner_id": thread["owner_id"], "thread_id": row["thread_id"], "turn_id": resolved,
        "run_id": f"chat-turn:{turn}", "project_id": thread["project_id"],
        "root_budget_id": row["root_budget_id"], "runtime_bundle_id": row["runtime_bundle_id"],
    }
    fields.update(overrides)
    return create_root_context(**fields)


# --------------------------------------------------------------------------- #
# P01: both upgrade paths reach the same head
# --------------------------------------------------------------------------- #

def test_p01_empty_database_upgrade_matches_the_declared_head(migrated_postgres_url) -> None:
    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
    assert version == POSTGRES_SCHEMA_HEAD
    for table in CONTEXT_TABLES:
        assert set(CONTEXT_COLUMNS) <= _columns(str(migrated_postgres_url), table)


def test_p01_upgrade_from_the_previous_head_keeps_legacy_rows_readable(
    migrated_postgres_url, fresh_database,
) -> None:
    # 1. The previous head: no execution-context columns yet.
    _alembic(fresh_database, PREVIOUS_HEAD)
    for table in CONTEXT_TABLES:
        assert not set(CONTEXT_COLUMNS) & _columns(fresh_database, table)

    # 2. Legacy rows written before the feature existed.
    thread_id, turn_id, call_id, invocation_id = "thread-legacy", "turn-legacy", "call-legacy", "inv-legacy"
    with psycopg.connect(fresh_database, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,version,next_event_seq,created_at,updated_at) "
            "VALUES (%s,'legacy','local-user',0,1,'2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')",
            (thread_id,),
        )
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,version,skill_names_json,created_at,updated_at) "
            "VALUES (%s,%s,'legacy-turn','COMPLETED',0,'[]','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')",
            (turn_id, thread_id),
        )
        connection.execute(
            "INSERT INTO turn_tool_calls(id,turn_id,thread_id,tool_name,params_json,params_hash,risk,status,"
            "binding_json,created_at) VALUES (%s,%s,%s,'get_today_tasks','{}','h','READ','EXECUTED','{}',"
            "'2026-09-01T00:00:00+00:00')",
            (call_id, turn_id, thread_id),
        )
        connection.execute(
            "INSERT INTO model_invocations(id,owner_id,role,purpose,routing_policy_digest,route_snapshot_json,"
            "request_digest,tool_schema_digest,context_snapshot_digest,status,idempotency_key,created_at) "
            "VALUES (%s,'local-user','conversation','route_and_respond','direct','{}','req','tools','snap',"
            "'SUCCEEDED','legacy-idem','2026-09-01T00:00:00+00:00')",
            (invocation_id,),
        )

    # 3. The upgrade adds the columns and leaves the legacy rows alone.
    _alembic(fresh_database, "head")
    with psycopg.connect(fresh_database, row_factory=dict_row) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        rows = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in CONTEXT_TABLES
        }
    assert version == POSTGRES_SCHEMA_HEAD
    for table in CONTEXT_TABLES:
        assert set(CONTEXT_COLUMNS) <= _columns(fresh_database, table)
        assert len(rows[table]) == 1
        for column in CONTEXT_COLUMNS:
            assert rows[table][0][column] is None

    # 4. A legacy record is readable by the application and fails closed on resume.
    db = Database(fresh_database, workspace=BACKEND_ROOT / "outputs" / "p01-legacy")
    try:
        store = HarnessContextStore(db)
        from app.execution_context import LegacyContextMissing

        with pytest.raises(LegacyContextMissing):
            store.load_turn_context(turn_id)
        with pytest.raises(LegacyContextMissing):
            store.load_tool_call_context(call_id)
        with pytest.raises(LegacyContextMissing):
            store.load_invocation_context(invocation_id)
        from app.conversation import ConversationService

        assert ConversationService(db).turn(turn_id).status == "COMPLETED"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# P02: round-trip on all three tables, plus legacy and half-written rows
# --------------------------------------------------------------------------- #

def test_p02_round_trip_and_legacy_and_half_written(migrated_postgres_url, tmp_path) -> None:
    from app.execution_context import LegacyContextMissing

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        store = HarnessContextStore(db)
        thread, accepted, call, invocation_id = _seed(db)
        turn_context = store.load_or_create_turn_context(
            accepted.turn_id, owner_id="local-user", thread_id=thread.id,
        )
        # The run scope is derived from the turn, never left to the caller.
        assert turn_context.run_id == f"chat-turn:{accepted.turn_id}"
        # A restart re-reads the same root instead of minting a new trace.
        assert store.load_turn_context(accepted.turn_id) == turn_context
        assert store.load_or_create_turn_context(
            accepted.turn_id, owner_id="local-user", thread_id=thread.id,
        ) == turn_context

        tool_context = create_child_context(turn_context)
        with db.transaction() as connection:
            store.save_tool_call_context(connection, call.id, tool_context)
        assert store.load_tool_call_context(call.id) == tool_context

        invocation_context = create_child_context(tool_context)
        with db.transaction() as connection:
            store.save_invocation_context(connection, invocation_id, invocation_context)
        assert store.load_invocation_context(invocation_id) == invocation_context

        # Digest and JSON are a pair on every row, and the digest recomputes.
        for table, row_id in (
            ("turns", accepted.turn_id), ("turn_tool_calls", call.id), ("model_invocations", invocation_id),
        ):
            with db.connection() as connection:
                row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
            assert row["execution_context_json"] is not None and row["execution_context_digest"] is not None
            assert execution_context_digest(row["execution_context_json"]) == row["execution_context_digest"]
            assert row["execution_context_digest"] != row["context_snapshot_digest"] if table == "model_invocations" else True

        # A half-written row is corrupt, never "half migrated".
        with db.transaction() as connection:
            connection.execute(
                "UPDATE turn_tool_calls SET execution_context_digest=NULL WHERE id=?", (call.id,),
            )
        with pytest.raises(ContextStoreConflict):
            store.load_tool_call_context(call.id)
        with pytest.raises(ContextStoreConflict):
            with db.transaction() as connection:
                store.save_tool_call_context(connection, call.id, tool_context)

        # A legacy row (both columns empty) is readable but has no context.
        legacy_call = "chat-tool-legacy"
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO turn_tool_calls(id,turn_id,thread_id,tool_name,params_json,params_hash,risk,status,"
                "binding_json,created_at) VALUES (?,?,?,'get_today_tasks','{}','h','READ','EXECUTED','{}',?)",
                (legacy_call, accepted.turn_id, thread.id, "2026-09-01T00:00:00+00:00"),
            )
        with pytest.raises(LegacyContextMissing):
            store.load_tool_call_context(legacy_call)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# P03: every binding, every table, plus concurrency
# --------------------------------------------------------------------------- #

def test_p03_conflicting_bindings_are_refused_on_every_table(migrated_postgres_url, tmp_path) -> None:
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        store = HarnessContextStore(db)
        thread, accepted, call, invocation_id = _seed(db)
        base = _context_for(db, accepted.turn_id)
        with db.transaction() as connection:
            store.save_turn_context(connection, accepted.turn_id, base)
        tool_context = create_child_context(base)
        with db.transaction() as connection:
            store.save_tool_call_context(connection, call.id, tool_context)
        invocation_context = create_child_context(tool_context)
        with db.transaction() as connection:
            store.save_invocation_context(connection, invocation_id, invocation_context)

        # A row already bound to a context can never be re-pointed, not even by a
        # derived child of that same context: the stored identity is immutable.
        for context, saver in (
            (base, lambda c, ctx: store.save_turn_context(c, accepted.turn_id, ctx)),
            (tool_context, lambda c, ctx: store.save_tool_call_context(c, call.id, ctx)),
            (invocation_context, lambda c, ctx: store.save_invocation_context(c, invocation_id, ctx)),
        ):
            with pytest.raises(ContextStoreConflict):
                with db.transaction() as connection:
                    saver(connection, create_child_context(context))
        # ...and the identical context is still idempotent.
        with db.transaction() as connection:
            store.save_turn_context(connection, accepted.turn_id, base)
            store.save_tool_call_context(connection, call.id, tool_context)
            store.save_invocation_context(connection, invocation_id, invocation_context)

        # Identity fields are rejected outright on each table.
        conflicts = {
            "turns": [
                _context_for(db, accepted.turn_id, owner_id="someone-else"),
                _context_for(db, accepted.turn_id, thread_id="thread-other"),
                _context_for(db, accepted.turn_id, turn_id="turn-other"),
                _context_for(db, accepted.turn_id, run_id="chat-turn:turn-other"),
                _context_for(db, accepted.turn_id, runtime_bundle_id="bundle-x"),
                _context_for(db, accepted.turn_id, root_budget_id="budget-x"),
            ],
        }
        for context in conflicts["turns"]:
            with pytest.raises(ContextStoreConflict):
                with db.transaction() as connection:
                    store.save_turn_context(connection, accepted.turn_id, context)

        tool_conflicts = [
            create_root_context(owner_id="someone-else", thread_id=thread.id, turn_id=accepted.turn_id),
            create_root_context(owner_id="local-user", thread_id="thread-other", turn_id=accepted.turn_id),
            create_root_context(owner_id="local-user", thread_id=thread.id, turn_id="turn-other"),
        ]
        for context in tool_conflicts:
            with pytest.raises(ContextStoreConflict):
                with db.transaction() as connection:
                    store.save_tool_call_context(connection, call.id, context)

        with db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_invocations WHERE id=?", (invocation_id,),
            ).fetchone()
        invocation_conflicts = [
            create_root_context(
                owner_id="someone-else", thread_id=row["thread_id"], turn_id=row["turn_id"],
                run_id=row["run_id"],
            ),
            create_root_context(
                owner_id=row["owner_id"], thread_id="thread-other", turn_id=row["turn_id"],
                run_id=row["run_id"],
            ),
            create_root_context(
                owner_id=row["owner_id"], thread_id=row["thread_id"], turn_id="turn-other",
                run_id=row["run_id"],
            ),
            create_root_context(
                owner_id=row["owner_id"], thread_id=row["thread_id"], turn_id=row["turn_id"],
                run_id="chat-turn:turn-other",
            ),
            create_root_context(
                owner_id=row["owner_id"], thread_id=row["thread_id"], turn_id=row["turn_id"],
                run_id=row["run_id"], task_id="task-unexpected",
            ),
        ]
        for context in invocation_conflicts:
            with pytest.raises(ContextStoreConflict):
                with db.transaction() as connection:
                    store.save_invocation_context(connection, invocation_id, context)

        # Nothing was silently overwritten.
        assert store.load_turn_context(accepted.turn_id) == base
        assert store.load_tool_call_context(call.id) == tool_context
        assert store.load_invocation_context(invocation_id) == invocation_context
    finally:
        db.close()


def test_p03_concurrent_writers_of_the_same_context_are_idempotent(
    migrated_postgres_url, tmp_path,
) -> None:
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        store = HarnessContextStore(db)
        thread, accepted, call, _ = _seed(db)
        context = _context_for(db, accepted.turn_id)
        with db.transaction() as connection:
            store.save_turn_context(connection, accepted.turn_id, context)

        barrier = Barrier(4)
        results: list[str] = []

        def save(_):
            barrier.wait()
            try:
                with db.transaction() as connection:
                    store.save_turn_context(connection, accepted.turn_id, context)
                return "ok"
            except HarnessContextError as exc:
                return type(exc).__name__

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(4)))
        # The same context is idempotent; nothing is overwritten and no partial
        # row is left behind.
        assert set(results) == {"ok"}, results
        assert store.load_turn_context(accepted.turn_id) == context

        # A *different* context racing the same row can never win.
        other = _context_for(db, accepted.turn_id, runtime_bundle_id="bundle-racer")

        def save_other(_):
            barrier2.wait()
            try:
                with db.transaction() as connection:
                    store.save_turn_context(connection, accepted.turn_id, other)
                return "ok"
            except HarnessContextError as exc:
                return type(exc).__name__

        barrier2 = Barrier(4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save_other, range(4)))
        assert set(results) == {"ContextStoreConflict"}, results
        assert store.load_turn_context(accepted.turn_id) == context
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# P04: injected failures and tampering
# --------------------------------------------------------------------------- #

def test_p04_failed_write_leaves_no_partial_row_and_tampering_is_refused(
    migrated_postgres_url, tmp_path, monkeypatch,
) -> None:
    from app import harness_context_store as module
    from app.conversation import ConversationService

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        store = HarnessContextStore(db)
        thread = ConversationService(db).create_thread("PG 篡改")
        accepted = ConversationService(db).accept_turn(
            thread.id, f"pg-{uuid.uuid4().hex[:8]}", "生成执行预览", [],
        )
        context = store.load_or_create_turn_context(
            accepted.turn_id, owner_id="local-user", thread_id=thread.id,
        )

        # 1. A failure while writing the context must not leave a partial row.
        def explode(_context):
            raise RuntimeError("simulated failure between JSON and digest")

        monkeypatch.setattr(module, "execution_context_digest", explode)
        with pytest.raises(RuntimeError, match="simulated failure"):
            with db.transaction() as connection:
                store.save_turn_context(connection, accepted.turn_id, context)
        monkeypatch.undo()

        with db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM turns WHERE id=?", (accepted.turn_id,),
            ).fetchone()
        assert row["execution_context_json"] is not None  # written by load_or_create
        assert row["execution_context_digest"] is not None

        # 2. Tampering with the stored JSON is refused.
        payload = serialize_context(context)
        tampered = payload.replace(context.span_id, "f" * 32)
        assert tampered != payload
        with db.transaction() as connection:
            connection.execute(
                "UPDATE turns SET execution_context_json=? WHERE id=?", (tampered, accepted.turn_id),
            )
        with pytest.raises(ContextStoreConflict):
            store.load_turn_context(accepted.turn_id)

        # 3. A self-consistent envelope (JSON and digest agree) whose identity
        #    contradicts the row is still refused: the binding check is
        #    independent of the digest, so a valid signature is not enough.
        impostor = create_root_context(
            owner_id="someone-else", thread_id=thread.id, turn_id=accepted.turn_id,
            run_id=f"chat-turn:{accepted.turn_id}",
        )
        impostor_payload = serialize_context(impostor)
        with db.transaction() as connection:
            connection.execute(
                "UPDATE turns SET execution_context_json=?, execution_context_digest=? WHERE id=?",
                (impostor_payload, execution_context_digest(impostor_payload), accepted.turn_id),
            )
        with pytest.raises(ContextStoreConflict):
            store.load_turn_context(accepted.turn_id)

        # 4. Restoring the original pair makes the row readable again.
        with db.transaction() as connection:
            connection.execute(
                "UPDATE turns SET execution_context_json=?, execution_context_digest=? WHERE id=?",
                (payload, execution_context_digest(payload), accepted.turn_id),
            )
        assert store.load_turn_context(accepted.turn_id) == context

        # 5. A row whose trusted source changed underneath the stored context is
        #    refused even though JSON and digest still agree.  The owner comes
        #    from ``threads``, so moving the thread is enough to break the binding.
        with db.transaction() as connection:
            connection.execute(
                "UPDATE threads SET owner_id=? WHERE id=?", ("someone-else", thread.id),
            )
        with pytest.raises(ContextStoreConflict):
            store.load_turn_context(accepted.turn_id)
    finally:
        db.close()


def test_p04_approval_context_is_written_before_the_approval_exists(
    migrated_postgres_url, tmp_path,
) -> None:
    """The pause must be atomic: no actionable approval without an identity."""
    from app.chat_tools import ChatToolCallStore

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        thread, accepted, _, _ = _seed(db)
        store = HarnessContextStore(db)
        context = store.load_or_create_turn_context(
            accepted.turn_id, owner_id="local-user", thread_id=thread.id,
        )
        tool_context = create_child_context(context)
        calls = ChatToolCallStore(db)
        created = calls.create(
            turn_id=accepted.turn_id, thread_id=thread.id, tool_name="create_plan_draft",
            params={"title": "t"}, risk="WRITE", status="PENDING_APPROVAL",
            execution_context=tool_context,
        )
        assert calls.load_execution_context(created.id) == tool_context
        with db.connection() as connection:
            row = connection.execute(
                "SELECT execution_context_json,execution_context_digest FROM turn_tool_calls WHERE id=?",
                (created.id,),
            ).fetchone()
        assert row["execution_context_json"] and row["execution_context_digest"]
    finally:
        db.close()


@pytest.mark.parametrize("table", CONTEXT_TABLES)
@pytest.mark.parametrize("same", [True, False])
def test_first_context_binding_race(migrated_postgres_url, tmp_path, table, same):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        store = HarnessContextStore(db)
        _, turn, call, invocation = _seed(db)
        first = _context_for(db, turn.turn_id)
        second = first if same else _context_for(db, turn.turn_id)
        save, load, key = {
            "turns": (store.save_turn_context, store.load_turn_context, turn.turn_id),
            "turn_tool_calls": (store.save_tool_call_context, store.load_tool_call_context, call.id),
            "model_invocations": (store.save_invocation_context, store.load_invocation_context, invocation),
        }[table]
        barrier = Barrier(2)
        original = store._assert_bindings
        def aligned(connection, name, row, context):
            original(connection, name, row, context)
            # Both transactions must observe the original empty row before
            # either tries its first write; a later re-read must not wait.
            if row["execution_context_json"] is None:
                barrier.wait(timeout=10)
        store._assert_bindings = aligned
        def write(context):
            try:
                with db.transaction() as connection:
                    save(connection, key, context)
                return "ok"
            except ContextStoreConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, [first, second]))
        store._assert_bindings = original
        assert sorted(results) == (["ok", "ok"] if same else ["conflict", "ok"])
        winner = first if results[0] == "ok" else second
        assert load(key) == winner
    finally:
        db.close()
