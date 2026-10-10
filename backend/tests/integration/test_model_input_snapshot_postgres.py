"""P01-P08: the frozen model input on the real PostgreSQL schema.

These cases cover what SQLite cannot: the Alembic upgrade path (an empty
database *and* a database sitting at the previous head), and real database-level
immutability, which has to hold even when someone bypasses the application and
writes SQL by hand.

Everything here goes through the isolated-database guard: ``migrated_postgres_url``
already refuses a non-test target before alembic runs, and the extra database
created for the upgrade-path case is checked the same way before it is migrated.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from psycopg.rows import dict_row

from app.db import POSTGRES_SCHEMA_HEAD, Database
from app.model_control import ModelCallContext
from app.model_gateway import ModelRequest
from app.model_input_snapshot import freeze_model_input
from db_target_guard import assert_isolated_test_database

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_HEAD = "20260930_0024"
# The revision 0024 replaced.  Pinned so a rewritten history is caught.
PREVIOUS_PREVIOUS_HEAD = "20260921_0023"
SNAPSHOT_COLUMNS = {
    "id", "owner_id", "schema_version", "content_json", "content_digest", "created_at",
}
SNAPSHOT_INDEXES = {"idx_model_input_snapshots_owner", "idx_model_input_snapshots_digest"}
SNAPSHOT_TRIGGERS = {"model_input_snapshots_append_only"}
BINDING_TRIGGERS = {"model_invocations_snapshot_binding_immutable"}


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
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _columns(url: str, table: str) -> set[str]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        return {
            row["column_name"] for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=%s", (table,),
            )
        }


def _tables(url: str) -> set[str]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        return {
            row["tablename"] for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
            )
        }


def _indexes(url: str, table: str) -> set[str]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        return {
            row["indexname"] for row in connection.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema() AND tablename=%s", (table,),
            )
        }


def _triggers(url: str, *tables: str) -> dict[str, str]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        rows = connection.execute(
            "SELECT tgname, pg_get_triggerdef(oid) AS definition FROM pg_trigger "
            "WHERE NOT tgisinternal AND tgrelid = ANY(%s::regclass[])",
            ([f"{table}" for table in tables],),
        ).fetchall()
    return {row["tgname"]: row["definition"] for row in rows}


def _check_constraints(url: str, table: str) -> dict[str, str]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        rows = connection.execute(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid=%s::regclass AND contype='c'",
            (table,),
        ).fetchall()
    return {row["conname"]: row["definition"] for row in rows}


def _foreign_keys(url: str, table: str) -> dict[str, str]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        rows = connection.execute(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid=%s::regclass AND contype='f'",
            (table,),
        ).fetchall()
    return {row["conname"]: row["definition"] for row in rows}


def _frozen(role: str = "conversation", purpose: str = "route_and_respond"):
    """A real frozen envelope, so the rows under test are not hand-written JSON."""
    request = ModelRequest(
        messages=[
            {"role": "system", "content": "你是助手。"},
            {"role": "user", "content": "第一问"},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "get_today_tasks",
                "description": "读取今日任务",
                "parameters": {"type": "object", "properties": {"date": {"type": "string"}}},
            },
        }],
        temperature=0.0,
        max_tokens=None,
    )
    context = ModelCallContext(
        role=role, purpose=purpose, owner_id="local-user", runtime_bundle_id=None,
    )
    return freeze_model_input(request, context)


def _insert_snapshot(connection, snapshot, *, snapshot_id: str, owner_id: str | None = None) -> str:
    connection.execute(
        "INSERT INTO model_input_snapshots(id,owner_id,schema_version,content_json,content_digest,created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            snapshot_id, owner_id or snapshot.owner_id, snapshot.schema_version,
            snapshot.content_json, snapshot.content_digest, "2026-09-30T00:00:00+00:00",
        ),
    )
    return snapshot_id


_OMIT = object()


def _insert_invocation(
    connection, *, invocation_id: str, owner_id: str = "local-user",
    context_snapshot_id=_OMIT, digest: str = "snap-digest",
) -> str:
    """Insert a call, optionally omitting the binding column.

    ``context_snapshot_id`` must be omittable: at the previous head the column
    does not exist yet, so the "historical row" in P01 has to be written without
    mentioning it.
    """
    columns = [
        "id", "owner_id", "role", "purpose", "routing_policy_digest", "route_snapshot_json",
        "request_digest", "tool_schema_digest", "context_snapshot_digest", "status",
        "idempotency_key", "created_at",
    ]
    values = [
        invocation_id, owner_id, "conversation", "route_and_respond", "direct", "{}",
        "req", "tools", digest, "RUNNING", f"idem-{uuid.uuid4().hex}",
        "2026-09-30T00:00:00+00:00",
    ]
    if context_snapshot_id is not _OMIT:
        columns.append("context_snapshot_id")
        values.append(context_snapshot_id)
    placeholders = ",".join(["%s"] * len(values))
    connection.execute(
        f"INSERT INTO model_invocations({','.join(columns)}) VALUES ({placeholders})",
        tuple(values),
    )
    return invocation_id


def _seed_turn(db: Database, *, owner_id: str = "local-user"):
    """A thread and a turn, so a real harness context can be rooted on one."""
    from app.conversation import ConversationService

    conversation = ConversationService(db)
    thread = conversation.create_thread("PG 快照", owner_id=owner_id)
    accepted = conversation.accept_turn(
        thread.id, f"pg-{uuid.uuid4().hex[:8]}", "生成执行预览", [], owner_id=owner_id,
    )
    return thread, accepted


def _context_for(db: Database, turn: str, **overrides):
    """A root harness context bound to ``turn``, with any field overridden."""
    from app.execution_context import create_root_context

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
# P01: upgrading a 0024 database that already holds invocations
# --------------------------------------------------------------------------- #

def test_p01_upgrade_from_the_previous_head_keeps_historical_calls_unbackfilled(
    migrated_postgres_url, fresh_database,
) -> None:
    # 1. The previous head: no snapshot table, no binding column.
    _alembic(fresh_database, PREVIOUS_HEAD)
    assert "model_input_snapshots" not in _tables(fresh_database)
    assert "context_snapshot_id" not in _columns(fresh_database, "model_invocations")

    # 2. A historical call that already carries the *old* digest column but has
    #    no snapshot behind it.  This is the shape rule 3.5/6 describes: the
    #    digest is populated, the ID cannot be.
    with psycopg.connect(fresh_database, autocommit=True) as connection:
        _insert_invocation(connection, invocation_id="inv-legacy", digest="legacy-digest")

    # 3. Upgrade.
    _alembic(fresh_database, "head")

    with psycopg.connect(fresh_database, row_factory=dict_row) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        row = connection.execute(
            "SELECT * FROM model_invocations WHERE id='inv-legacy'"
        ).fetchone()
        snapshots = connection.execute(
            "SELECT COUNT(*) AS count FROM model_input_snapshots"
        ).fetchone()["count"]

    assert version == POSTGRES_SCHEMA_HEAD
    # The historical call survives untouched: no snapshot was fabricated for it,
    # and the digest it always had was not rewritten.
    assert row is not None
    assert row["context_snapshot_id"] is None
    assert row["context_snapshot_digest"] == "legacy-digest"
    assert snapshots == 0

    # The new binding column is real and enforced after the upgrade.
    assert SNAPSHOT_COLUMNS <= _columns(fresh_database, "model_input_snapshots")
    assert "context_snapshot_id" in _columns(fresh_database, "model_invocations")
    # The upgrade only added the column; it did not silently bind the old row.
    assert _foreign_keys(fresh_database, "model_invocations")


# --------------------------------------------------------------------------- #
# P02: a from-scratch PostgreSQL database, and the declared head
# --------------------------------------------------------------------------- #

def test_p02_empty_database_upgrade_matches_the_declared_head(migrated_postgres_url) -> None:
    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()["version_num"]

    assert version == POSTGRES_SCHEMA_HEAD
    assert SNAPSHOT_COLUMNS <= _columns(str(migrated_postgres_url), "model_input_snapshots")
    assert SNAPSHOT_INDEXES <= _indexes(str(migrated_postgres_url), "model_input_snapshots")


def test_p02_snapshot_table_carries_its_constraints_and_binding_trigger(
    migrated_postgres_url,
) -> None:
    url = str(migrated_postgres_url)

    checks = _check_constraints(url, "model_input_snapshots")
    assert any("schema_version" in definition for definition in checks.values()), checks
    assert any("content_digest" in definition for definition in checks.values()), checks

    foreign_keys = _foreign_keys(url, "model_invocations")
    assert any(
        "model_input_snapshots" in definition and "context_snapshot_id" in definition
        for definition in foreign_keys.values()
    ), foreign_keys

    triggers = _triggers(url, "model_input_snapshots", "model_invocations")
    assert SNAPSHOT_TRIGGERS <= set(triggers)
    assert BINDING_TRIGGERS <= set(triggers)
    # The binding trigger must be column-scoped, otherwise ordinary status and
    # attempt updates would be blocked too (rule 3.5/4).
    assert "UPDATE OF context_snapshot_id" in triggers[
        "model_invocations_snapshot_binding_immutable"
    ]


def test_p02_the_declared_head_is_reachable_without_rewriting_history() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [POSTGRES_SCHEMA_HEAD]

    revisions = {revision.revision: revision.down_revision for revision in script.walk_revisions()}
    assert revisions["20260930_0025"] == PREVIOUS_HEAD
    assert revisions["20261009_0026"] == "20260930_0025"
    assert revisions[POSTGRES_SCHEMA_HEAD] == "20261009_0026"
    assert revisions[PREVIOUS_HEAD] == PREVIOUS_PREVIOUS_HEAD
    # Only the one new revision may point at the old head; if an older migration
    # had been edited to chain differently, more than one parent would appear.
    assert list(revisions.values()).count(PREVIOUS_HEAD) == 1


# --------------------------------------------------------------------------- #
# P07: immutability enforced by the database, not by Python attributes
# --------------------------------------------------------------------------- #

def test_p07_direct_sql_cannot_rewrite_or_unbind_a_snapshot(
    migrated_postgres_url,
) -> None:
    snapshot = _frozen()
    url = str(migrated_postgres_url)

    with psycopg.connect(url, autocommit=True) as connection:
        _insert_snapshot(connection, snapshot, snapshot_id="snap-1")
        _insert_snapshot(connection, snapshot, snapshot_id="snap-2")
        _insert_invocation(
            connection, invocation_id="inv-bound", context_snapshot_id="snap-1",
        )

    # 1. The snapshot's own fields are frozen.
    for statement, parameters in (
        ("UPDATE model_input_snapshots SET content_json=%s WHERE id='snap-1'", ('{"tampered":true}',)),
        ("UPDATE model_input_snapshots SET content_digest=%s WHERE id='snap-1'", ("0" * 64,)),
        ("UPDATE model_input_snapshots SET owner_id=%s WHERE id='snap-1'", ("someone-else",)),
        ("UPDATE model_input_snapshots SET schema_version=%s WHERE id='snap-1'", ("model-input-snapshot-v9",)),
        ("DELETE FROM model_input_snapshots WHERE id='snap-1'", ()),
    ):
        with psycopg.connect(url, autocommit=True) as connection:
            with pytest.raises(psycopg.errors.RaiseException, match="immutable|append-only"):
                connection.execute(statement, parameters)

    # 2. The binding cannot be replaced or cleared, and the database refuses it
    #    before the application ever gets a chance to.
    for statement, parameters in (
        ("UPDATE model_invocations SET context_snapshot_id=NULL WHERE id='inv-bound'", ()),
        ("UPDATE model_invocations SET context_snapshot_id='snap-2' WHERE id='inv-bound'", ()),
    ):
        with psycopg.connect(url, autocommit=True) as connection:
            with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                connection.execute(statement, parameters)

    # 2b. A call created without an input stays that way.  The binding is only
    #     ever written by the invocation's own INSERT, so there is no window in
    #     which it could be filled in later — back-filling would fabricate a
    #     record of what a call sent.
    with psycopg.connect(url, autocommit=True) as connection:
        _insert_invocation(connection, invocation_id="inv-open")
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            connection.execute(
                "UPDATE model_invocations SET context_snapshot_id='snap-2' WHERE id='inv-open'"
            )
        row = connection.execute(
            "SELECT context_snapshot_id FROM model_invocations WHERE id='inv-open'"
        ).fetchone()
        assert row[0] is None

    # 3. The invocation state machine still runs: the trigger is scoped to the
    #    binding column only.
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(
            "UPDATE model_invocations SET status='SUCCEEDED', finished_at='2026-09-30T00:01:00+00:00' "
            "WHERE id='inv-bound'"
        )
        connection.execute(
            "UPDATE model_invocations SET root_budget_id=NULL WHERE id='inv-bound'"
        )
        row = connection.execute(
            "SELECT status, context_snapshot_id, context_snapshot_digest FROM model_invocations "
            "WHERE id='inv-bound'"
        ).fetchone()

    assert row[0] == "SUCCEEDED"
    assert row[1] == "snap-1"
    assert row[2] == "snap-digest"

    # 4. Value-level constraints reject an envelope the code could never write.
    for values in (
        ("snap-bad-version", "local-user", "model-input-snapshot-v9", "{}", "a" * 64),
        ("snap-bad-digest", "local-user", "model-input-snapshot-v1", "{}", "short"),
    ):
        with psycopg.connect(url, autocommit=True) as connection:
            with pytest.raises(psycopg.errors.CheckViolation):
                connection.execute(
                    "INSERT INTO model_input_snapshots"
                    "(id,owner_id,schema_version,content_json,content_digest,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,'2026-09-30T00:00:00+00:00')",
                    values,
                )

    # 5. A binding that points at nothing is refused by the foreign key.
    with psycopg.connect(url, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_invocation(
                connection, invocation_id="inv-orphan", context_snapshot_id="snap-missing",
            )


# --------------------------------------------------------------------------- #
# P04: the store refuses corruption and impersonation on the real schema
# --------------------------------------------------------------------------- #

def test_p04_store_refuses_corrupt_and_impersonating_rows(migrated_postgres_url, tmp_path) -> None:
    from app.model_input_snapshot import SnapshotBindingConflict, SnapshotIntegrityError
    from app.model_input_snapshot_store import ModelInputSnapshotStore, SnapshotMissing

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        store = ModelInputSnapshotStore(db)
        snapshot = _frozen()
        with db.transaction() as connection:
            snapshot_id = store.insert(connection, snapshot, snapshot_id="snap-good")
        loaded = store.load("local-user", snapshot_id)
        assert loaded.content_digest == snapshot.content_digest
        assert loaded.to_request().messages == snapshot.to_request().messages

        # A row whose digest does not describe its content is corrupt, and it is
        # never reported as a legacy record instead.
        with psycopg.connect(str(migrated_postgres_url), autocommit=True) as connection:
            connection.execute(
                "INSERT INTO model_input_snapshots"
                "(id,owner_id,schema_version,content_json,content_digest,created_at) "
                "VALUES ('snap-corrupt','local-user','model-input-snapshot-v1',%s,%s,"
                "'2026-09-30T00:00:00+00:00')",
                (snapshot.content_json, "0" * 64),
            )
            _insert_invocation(
                connection, invocation_id="inv-corrupt",
                context_snapshot_id="snap-corrupt", digest="0" * 64,
            )
        with pytest.raises(SnapshotIntegrityError):
            store.load("local-user", "snap-corrupt")
        with pytest.raises(SnapshotIntegrityError):
            store.load_for_invocation("local-user", "inv-corrupt")

        # A row whose envelope disagrees with its owner column is refused, and it
        # is invisible to the owner the envelope claims.
        with psycopg.connect(str(migrated_postgres_url), autocommit=True) as connection:
            connection.execute(
                "INSERT INTO model_input_snapshots"
                "(id,owner_id,schema_version,content_json,content_digest,created_at) "
                "VALUES ('snap-swapped','someone-else','model-input-snapshot-v1',%s,%s,"
                "'2026-09-30T00:00:00+00:00')",
                (snapshot.content_json, snapshot.content_digest),
            )
        with pytest.raises(SnapshotBindingConflict):
            store.load("someone-else", "snap-swapped")
        with pytest.raises(SnapshotMissing):
            store.load("local-user", "snap-swapped")

        # A self-consistent snapshot for another role cannot be bound to this call.
        other = _frozen(role="planner", purpose="plan")
        with db.transaction() as connection:
            other_id = store.insert(connection, other, snapshot_id="snap-planner")
        with psycopg.connect(str(migrated_postgres_url), autocommit=True) as connection:
            _insert_invocation(connection, invocation_id="inv-conversation")
        # The store will not hand it over for this call's identity...
        with db.transaction() as connection:
            with pytest.raises(SnapshotBindingConflict):
                store.require_bindable(
                    connection, other_id, owner_id="local-user",
                    runtime_bundle_id=None, role="conversation", purpose="route_and_respond",
                )
        # ...and the database refuses to attach it to the existing call at all,
        # so the mismatch cannot be recorded even by hand.
        with psycopg.connect(str(migrated_postgres_url), autocommit=True) as connection:
            with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                connection.execute(
                    "UPDATE model_invocations SET context_snapshot_id=%s, context_snapshot_digest=%s "
                    "WHERE id='inv-conversation'",
                    (other_id, other.content_digest),
                )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# P06: concurrent writers of one idempotency key
# --------------------------------------------------------------------------- #

def test_p06_concurrent_calls_on_one_idempotency_key_have_exactly_one_winner(
    migrated_postgres_url, tmp_path,
) -> None:
    from app.model_control import (
        InvocationIdempotencyConflict, InvocationReplayError, ModelControlStore,
    )
    from app.model_gateway import ModelProfile

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        control = ModelControlStore(db)
        profile = ModelProfile("https://provider.test/v1", "demo", "MODEL_KEY", max_attempts=1)
        request = ModelRequest(messages=[{"role": "user", "content": "第一问"}])

        # Warm the profile rows up first.  `begin_invocation` writes them with
        # `ON CONFLICT DO NOTHING`, and two transactions racing that very first
        # insert would serialise before they ever reached the key check this test
        # is about - the barrier would then time out instead of overlapping.
        warmup = ModelCallContext(
            role="conversation", purpose="route_and_respond", owner_id="local-user",
            idempotency_key="pg-warmup",
        )
        control.begin_invocation(
            profile, request, warmup, snapshot=freeze_model_input(request, warmup),
        )

        context = ModelCallContext(
            role="conversation", purpose="route_and_respond", owner_id="local-user",
            idempotency_key="pg-race-key",
        )
        snapshot = freeze_model_input(request, context)

        # Both writers must pass the "key not taken yet" check before either
        # commits, otherwise the race is not actually being tested.  Only the
        # first two lookups - one per writer - wait at the barrier; the loser's
        # post-conflict re-read must not.
        barrier = Barrier(2)
        original = ModelControlStore._existing_invocation
        state = {"n": 0}
        guard = threading.Lock()

        def aligned(self, connection, key):
            row = original(self, connection, key)
            if key == "pg-race-key":
                with guard:
                    state["n"] += 1
                    ordinal = state["n"]
                if ordinal <= 2:
                    barrier.wait(timeout=15)
            return row

        ModelControlStore._existing_invocation = aligned

        def open_call(_: int):
            try:
                handle = control.begin_invocation(profile, request, context, snapshot=snapshot)
                return ("created", handle.invocation_id)
            except InvocationReplayError as exc:
                return ("replay", exc.invocation_id)
            except InvocationIdempotencyConflict as exc:
                return ("conflict", str(exc))

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(open_call, range(2)))
        finally:
            ModelControlStore._existing_invocation = original

        # Exactly one call exists; the loser is told it is a replay, not handed
        # the driver's constraint violation.
        assert sorted(kind for kind, _ in results) == ["created", "replay"], results
        created_id = next(value for kind, value in results if kind == "created")

        with psycopg.connect(str(migrated_postgres_url), row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT id, context_snapshot_id FROM model_invocations "
                "WHERE idempotency_key='pg-race-key'"
            ).fetchall()
            orphans = connection.execute(
                "SELECT COUNT(*) AS n FROM model_input_snapshots s WHERE NOT EXISTS "
                "(SELECT 1 FROM model_invocations i WHERE i.context_snapshot_id = s.id)"
            ).fetchone()["n"]
        assert len(rows) == 1
        assert rows[0]["id"] == created_id
        assert rows[0]["context_snapshot_id"] is not None
        # The loser's rollback left no snapshot behind that no call points at.
        assert orphans == 0
    finally:
        db.close()


def test_p06_a_reused_key_with_different_input_or_identity_is_a_conflict(
    migrated_postgres_url, tmp_path,
) -> None:
    from app.model_control import (
        InvocationIdempotencyConflict, InvocationReplayError, ModelControlStore,
    )
    from app.model_gateway import ModelProfile

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        control = ModelControlStore(db)
        profile = ModelProfile("https://provider.test/v1", "demo", "MODEL_KEY", max_attempts=1)
        request = ModelRequest(messages=[{"role": "user", "content": "第一问"}])
        context = ModelCallContext(
            role="conversation", purpose="route_and_respond", owner_id="local-user",
            idempotency_key="pg-key",
        )
        first = control.begin_invocation(
            profile, request, context, snapshot=freeze_model_input(request, context),
        )

        # Same key, same input, same identity: a replay, not a second call.
        with pytest.raises(InvocationReplayError) as replay:
            control.begin_invocation(
                profile, request, context, snapshot=freeze_model_input(request, context),
            )
        assert replay.value.invocation_id == first.invocation_id

        # Same key, different input: refused rather than answered with the first
        # call's result.
        changed = ModelRequest(messages=[{"role": "user", "content": "另一问"}])
        with pytest.raises(InvocationIdempotencyConflict):
            control.begin_invocation(
                profile, changed, context, snapshot=freeze_model_input(changed, context),
            )

        # Same key and same bytes, different owner: still a different call.
        other_owner = ModelCallContext(
            role="conversation", purpose="route_and_respond", owner_id="someone-else",
            idempotency_key="pg-key",
        )
        with pytest.raises(InvocationIdempotencyConflict):
            control.begin_invocation(
                profile, request, other_owner,
                snapshot=freeze_model_input(request, other_owner),
            )

        with psycopg.connect(str(migrated_postgres_url), row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT id, owner_id FROM model_invocations WHERE idempotency_key='pg-key'"
            ).fetchall()
        assert rows == [{"id": first.invocation_id, "owner_id": "local-user"}]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# K05-K06: concurrent writers of one key, told apart by execution identity
#
# F03 (independent review, 2026-10-06): the identity check only ran when *both*
# sides carried a harness digest, so a key re-used across two different
# executions was answered as a replay.  These two cases race the writers for
# real on PostgreSQL and pin what the loser is told: the same execution replays,
# a different one is a domain conflict - never the driver's constraint error.
# --------------------------------------------------------------------------- #

def _race_one_key(control, profile, request, contexts, snapshots, key):
    """Run every context through ``begin_invocation`` with the key checks overlapped.

    The barrier sits on the "is this key taken?" read, so both writers see an
    empty key before either commits.  Only the first ``len(contexts)`` reads wait;
    the loser's post-conflict re-read must not, or the test would deadlock.
    """
    from app.model_control import (
        InvocationIdempotencyConflict, InvocationReplayError, ModelControlStore,
    )

    barrier = Barrier(len(contexts))
    original = ModelControlStore._existing_invocation
    state = {"n": 0}
    guard = threading.Lock()

    def aligned(self, connection, lookup_key):
        row = original(self, connection, lookup_key)
        if lookup_key == key:
            with guard:
                state["n"] += 1
                ordinal = state["n"]
            if ordinal <= len(contexts):
                barrier.wait(timeout=15)
        return row

    ModelControlStore._existing_invocation = aligned

    def open_call(index):
        try:
            handle = control.begin_invocation(
                profile, request, contexts[index], snapshot=snapshots[index],
            )
            return ("created", handle.invocation_id)
        except InvocationReplayError as exc:
            return ("replay", exc.invocation_id)
        except InvocationIdempotencyConflict as exc:
            return ("conflict", str(exc))

    try:
        with ThreadPoolExecutor(max_workers=len(contexts)) as pool:
            return list(pool.map(open_call, range(len(contexts))))
    finally:
        ModelControlStore._existing_invocation = original


def _harness_race_setup(control, *, key: str, harness, second_harness=None):
    """Warm the profile rows, then build one or two contexts sharing ``key``.

    ``second_harness`` lets the caller give the two writers *different* execution
    identities while keeping every public column identical.
    """
    from app.model_control import ModelCallContext

    request = _call_request()
    warmup = ModelCallContext.from_harness(
        harness, role="conversation", purpose="route_and_respond",
        idempotency_key=f"{key}-warmup",
    )
    control.begin_invocation(
        _profile(), request, warmup, snapshot=freeze_model_input(request, warmup),
    )
    first = ModelCallContext.from_harness(
        harness, role="conversation", purpose="route_and_respond", idempotency_key=key,
    )
    second = ModelCallContext.from_harness(
        second_harness or harness,
        role="conversation", purpose="route_and_respond", idempotency_key=key,
    )
    return request, first, second


def test_k05_concurrent_writers_with_the_same_identity_have_one_winner(
    migrated_postgres_url, tmp_path,
) -> None:
    from app.model_control import ModelControlStore

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        control = ModelControlStore(db)
        thread, accepted = _seed_turn(db)
        harness = _context_for(db, accepted.turn_id)
        request, first, second = _harness_race_setup(
            control, key="pg-race-k05", harness=harness,
        )

        results = _race_one_key(
            control, _profile(), request, [first, second],
            [freeze_model_input(request, first), freeze_model_input(request, second)],
            "pg-race-k05",
        )

        # Exactly one call is created; the other is told it is a replay of that
        # call, not handed a second row or a constraint error.
        assert sorted(kind for kind, _ in results) == ["created", "replay"], results
        created_id = next(value for kind, value in results if kind == "created")
        replay_id = next(value for kind, value in results if kind == "replay")
        assert replay_id == created_id

        with psycopg.connect(str(migrated_postgres_url), row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT id, context_snapshot_id FROM model_invocations "
                "WHERE idempotency_key='pg-race-k05'"
            ).fetchall()
            orphans = connection.execute(
                "SELECT COUNT(*) AS n FROM model_input_snapshots s WHERE NOT EXISTS "
                "(SELECT 1 FROM model_invocations i WHERE i.context_snapshot_id = s.id)"
            ).fetchone()["n"]
        assert len(rows) == 1
        assert rows[0]["id"] == created_id
        assert rows[0]["context_snapshot_id"] is not None
        # The loser rolled back its frozen input too: no snapshot dangles.
        assert orphans == 0
    finally:
        db.close()


def test_k06_concurrent_writers_with_different_execution_identities_conflict(
    migrated_postgres_url, tmp_path,
) -> None:
    from app.model_control import ModelControlStore

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        control = ModelControlStore(db)
        # Same owner/run/thread/turn/budget/bundle, but a second root context -
        # a different span, hence a different execution identity.
        thread, accepted = _seed_turn(db)
        harness = _context_for(db, accepted.turn_id)
        other_harness = _context_for(db, accepted.turn_id)
        request, first, second = _harness_race_setup(
            control, key="pg-race-k06", harness=harness, second_harness=other_harness,
        )
        # The two writers agree on every public column...
        for column, _label in ModelControlStore._IDENTITY_COLUMNS:
            assert getattr(first, column) == getattr(second, column), column
        # ...and differ only in the execution identity they claim.
        assert first.span_id != second.span_id

        results = _race_one_key(
            control, _profile(), request, [first, second],
            [freeze_model_input(request, first), freeze_model_input(request, second)],
            "pg-race-k06",
        )

        # One wins; the other is a *domain* conflict, reported as a differing
        # execution identity rather than a leaked database constraint error.
        assert sorted(kind for kind, _ in results) == ["conflict", "created"], results
        conflict = next(value for kind, value in results if kind == "conflict")
        assert "execution identity" in conflict, conflict
        for leak in ("psycopg", "UniqueViolation", "duplicate key", "IntegrityError"):
            assert leak not in conflict, conflict

        with psycopg.connect(str(migrated_postgres_url), row_factory=dict_row) as connection:
            rows = connection.execute(
                "SELECT id, context_snapshot_id, execution_context_digest "
                "FROM model_invocations WHERE idempotency_key='pg-race-k06'"
            ).fetchall()
            orphans = connection.execute(
                "SELECT COUNT(*) AS n FROM model_input_snapshots s WHERE NOT EXISTS "
                "(SELECT 1 FROM model_invocations i WHERE i.context_snapshot_id = s.id)"
            ).fetchone()["n"]
        # The winner's row is intact and still carries the execution identity it
        # was opened under; nothing was overwritten.
        assert len(rows) == 1
        assert rows[0]["context_snapshot_id"] is not None
        assert rows[0]["execution_context_digest"] is not None
        assert orphans == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# P08: identical input, independent rows, no cross-owner reads
# --------------------------------------------------------------------------- #

def test_p08_identical_input_shares_a_digest_but_not_a_row(migrated_postgres_url, tmp_path) -> None:
    from app.model_input_snapshot_store import ModelInputSnapshotStore, SnapshotMissing

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        store = ModelInputSnapshotStore(db)
        snapshot = _frozen()
        with db.transaction() as connection:
            first = store.insert(connection, snapshot, snapshot_id="snap-1")
            second = store.insert(connection, snapshot, snapshot_id="snap-2")
        with psycopg.connect(str(migrated_postgres_url), autocommit=True) as connection:
            _insert_invocation(connection, invocation_id="inv-1", context_snapshot_id=first,
                               digest=snapshot.content_digest)
            _insert_invocation(connection, invocation_id="inv-2", context_snapshot_id=second,
                               digest=snapshot.content_digest)

        one = store.load_for_invocation("local-user", "inv-1")
        two = store.load_for_invocation("local-user", "inv-2")
        assert one.id == "snap-1" and two.id == "snap-2"
        assert one.content_digest == two.content_digest == snapshot.content_digest

        with psycopg.connect(str(migrated_postgres_url), row_factory=dict_row) as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM model_input_snapshots"
            ).fetchone()["count"]
        assert count == 2

        with pytest.raises(SnapshotMissing) as excinfo:
            store.load("someone-else", first)
        assert "第一问" not in str(excinfo.value)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# P05 (transaction boundary): the core three commit or roll back together
# --------------------------------------------------------------------------- #

def _profile():
    from app.model_gateway import ModelProfile

    return ModelProfile("https://provider.test/v1", "demo", "MODEL_KEY", max_attempts=1)


def _call_request():
    return ModelRequest(messages=[{"role": "user", "content": "今天要做什么？"}])


def _counts(url: str) -> dict[str, int]:
    with psycopg.connect(url, row_factory=dict_row) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in ("model_input_snapshots", "model_invocations", "model_attempts")
        }


def test_p05_a_failed_snapshot_write_rolls_back_the_invocation(
    migrated_postgres_url, tmp_path, monkeypatch,
) -> None:
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_input_snapshot import freeze_model_input

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        thread, accepted = _seed_turn(db)
        harness = _context_for(db, accepted.turn_id)
        context = ModelCallContext.from_harness(
            harness, role="conversation", purpose="route_and_respond",
        )
        request = _call_request()
        snapshot = freeze_model_input(request, context)
        control = ModelControlStore(db)

        def explode(*args, **kwargs):
            raise RuntimeError("simulated snapshot persistence failure")

        monkeypatch.setattr(control.snapshots, "insert", explode)
        with pytest.raises(RuntimeError, match="simulated"):
            control.begin_invocation(_profile(), request, context, snapshot=snapshot)

        assert _counts(str(migrated_postgres_url)) == {
            "model_input_snapshots": 0, "model_invocations": 0, "model_attempts": 0,
        }
    finally:
        db.close()


def test_p05_a_failed_execution_context_write_rolls_back_snapshot_and_invocation(
    migrated_postgres_url, tmp_path, monkeypatch,
) -> None:
    """The binding is written after the snapshot, so it proves the whole group."""
    from app.harness_context_store import HarnessContextStore
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_input_snapshot import freeze_model_input

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        thread, accepted = _seed_turn(db)
        harness = _context_for(db, accepted.turn_id)
        context = ModelCallContext.from_harness(
            harness, role="conversation", purpose="route_and_respond",
        )
        request = _call_request()
        snapshot = freeze_model_input(request, context)
        control = ModelControlStore(db)

        def explode(self, connection, invocation_id, context_value):
            raise RuntimeError("simulated execution context failure")

        monkeypatch.setattr(HarnessContextStore, "save_invocation_context", explode)
        with pytest.raises(RuntimeError, match="simulated"):
            control.begin_invocation(_profile(), request, context, snapshot=snapshot)

        assert _counts(str(migrated_postgres_url)) == {
            "model_input_snapshots": 0, "model_invocations": 0, "model_attempts": 0,
        }
    finally:
        db.close()


def test_p05_a_committed_call_can_be_read_back_through_the_snapshot(
    migrated_postgres_url, tmp_path,
) -> None:
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_input_snapshot import freeze_model_input
    from app.model_input_snapshot_store import ModelInputSnapshotStore

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        thread, accepted = _seed_turn(db)
        harness = _context_for(db, accepted.turn_id)
        context = ModelCallContext.from_harness(
            harness, role="conversation", purpose="route_and_respond",
        )
        request = _call_request()
        snapshot = freeze_model_input(request, context)
        control = ModelControlStore(db)

        handle = control.begin_invocation(_profile(), request, context, snapshot=snapshot)

        assert _counts(str(migrated_postgres_url)) == {
            "model_input_snapshots": 1, "model_invocations": 1, "model_attempts": 0,
        }
        stored = ModelInputSnapshotStore(db).load_for_invocation("local-user", handle.invocation_id)
        assert stored.content_digest == snapshot.content_digest
        assert stored.to_request().messages == request.messages
        with psycopg.connect(str(migrated_postgres_url), row_factory=dict_row) as connection:
            row = connection.execute(
                "SELECT execution_context_json, context_snapshot_id, context_snapshot_digest "
                "FROM model_invocations WHERE id=%s", (handle.invocation_id,),
            ).fetchone()
        # All three landed in the one transaction.
        assert row["execution_context_json"] is not None
        assert row["context_snapshot_id"] == handle.input_snapshot_id
        assert row["context_snapshot_digest"] == snapshot.content_digest
    finally:
        db.close()
