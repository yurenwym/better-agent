"""The snapshot store's rules, verified on SQLite.

These are the pure storage rules from the P section: a row that was tampered with
is refused rather than downgraded to "legacy", a self-consistent envelope still
cannot impersonate another call, and a binding is written once.  The PostgreSQL
counterpart lives in ``tests/integration/test_model_input_snapshot_postgres.py``.
"""
from __future__ import annotations

import json

import pytest

from app.db import Database
from app.model_control import ModelCallContext
from app.model_gateway import ModelRequest
from app.model_input_snapshot import (
    ModelInputSnapshot,
    SnapshotBindingConflict,
    SnapshotIntegrityError,
    UnknownSnapshotVersion,
    canonical_json,
    freeze_model_input,
)
from app.model_input_snapshot_store import (
    ModelInputSnapshotStore,
    ModelInvocationMissing,
    SnapshotBindingMissing,
    SnapshotMissing,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _request(**overrides) -> ModelRequest:
    values = {
        "messages": [
            {"role": "system", "content": "你是助手。"},
            {"role": "user", "content": "第一问"},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_today_tasks",
                "description": "读取今日任务",
                "parameters": {"type": "object", "properties": {"date": {"type": "string"}}},
            },
        }],
        "temperature": 0.0,
        "max_tokens": None,
    }
    values.update(overrides)
    return ModelRequest(**values)


def _context(**overrides) -> ModelCallContext:
    values = {
        "role": "conversation",
        "purpose": "route_and_respond",
        "owner_id": "owner-a",
        "runtime_bundle_id": "bundle-1",
    }
    values.update(overrides)
    return ModelCallContext(**values)


def _invocation(
    db: Database, *, invocation_id: str, owner_id: str = "owner-a",
    role: str = "conversation", purpose: str = "route_and_respond",
    runtime_bundle_id: str | None = "bundle-1", status: str = "RUNNING",
    finished_at: str | None = None, snapshot_id: str | None = None,
    digest: str = "",
) -> None:
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_invocations(id,owner_id,role,purpose,runtime_bundle_id,"
            "routing_policy_digest,route_snapshot_json,request_digest,tool_schema_digest,"
            "context_snapshot_digest,status,idempotency_key,created_at,finished_at,context_snapshot_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                invocation_id, owner_id, role, purpose, runtime_bundle_id, "direct", "{}",
                "req", "tools", digest, status, f"idem-{invocation_id}", "now", finished_at,
                snapshot_id,
            ),
        )


def _store(tmp_path) -> tuple[Database, ModelInputSnapshotStore]:
    db = Database(tmp_path / "snapshots.db")
    return db, ModelInputSnapshotStore(db)


def _insert(db: Database, store: ModelInputSnapshotStore, snapshot, *, snapshot_id=None) -> str:
    with db.transaction() as connection:
        return store.insert(connection, snapshot, snapshot_id=snapshot_id)


# --------------------------------------------------------------------------- #
# round trip
# --------------------------------------------------------------------------- #

def test_round_trip_returns_the_frozen_content_and_recomputes_the_digest(tmp_path) -> None:
    db, store = _store(tmp_path)
    snapshot = freeze_model_input(_request(), _context())
    snapshot_id = _insert(db, store, snapshot, snapshot_id="snap-1")

    loaded = store.load("owner-a", "snap-1")
    assert loaded.id == snapshot_id
    assert loaded.content_digest == snapshot.content_digest
    assert loaded.content_json == snapshot.content_json
    assert loaded.owner_id == "owner-a"
    assert loaded.role == "conversation"
    assert loaded.runtime_bundle_id == "bundle-1"
    assert loaded.to_request().messages == _request().messages
    assert loaded.provenance().status == "partial"

    _invocation(db, invocation_id="inv-1", snapshot_id=snapshot_id, digest=snapshot.content_digest)

    bound = store.load_for_invocation("owner-a", "inv-1")
    assert bound.content_digest == snapshot.content_digest
    with db.connection() as connection:
        row = connection.execute(
            "SELECT context_snapshot_digest FROM model_invocations WHERE id='inv-1'"
        ).fetchone()
    # The invocation's reused digest column now describes the snapshot it is bound to.
    assert row["context_snapshot_digest"] == snapshot.content_digest


# --------------------------------------------------------------------------- #
# P04: tampering and identity conflicts
# --------------------------------------------------------------------------- #

def test_p04_a_row_whose_digest_does_not_match_its_content_is_corrupt(tmp_path) -> None:
    db, store = _store(tmp_path)
    snapshot = freeze_model_input(_request(), _context())
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_input_snapshots"
            "(id,owner_id,schema_version,content_json,content_digest,created_at) VALUES (?,?,?,?,?,?)",
            ("snap-corrupt", "owner-a", "model-input-snapshot-v1", snapshot.content_json, "0" * 64, "now"),
        )

    with pytest.raises(SnapshotIntegrityError):
        store.load("owner-a", "snap-corrupt")

    # ...and it must not be reported as a legacy record either.
    _invocation(db, invocation_id="inv-corrupt", snapshot_id="snap-corrupt", digest="0" * 64)
    with pytest.raises(SnapshotIntegrityError):
        store.load_for_invocation("owner-a", "inv-corrupt")


def test_p04_a_row_whose_envelope_disagrees_with_its_owner_column_is_refused(tmp_path) -> None:
    db, store = _store(tmp_path)
    snapshot = freeze_model_input(_request(), _context(owner_id="owner-a"))
    with db.transaction() as connection:
        # The column says owner-b, the envelope says owner-a.  Both are internally
        # fine on their own, which is exactly why the pair has to be checked.
        connection.execute(
            "INSERT INTO model_input_snapshots"
            "(id,owner_id,schema_version,content_json,content_digest,created_at) VALUES (?,?,?,?,?,?)",
            ("snap-swapped", "owner-b", "model-input-snapshot-v1", snapshot.content_json, snapshot.content_digest, "now"),
        )

    with pytest.raises(SnapshotBindingConflict):
        store.load("owner-b", "snap-swapped")
    # And it is invisible to the owner the envelope claims.
    with pytest.raises(SnapshotMissing):
        store.load("owner-a", "snap-swapped")


def test_p04_a_self_consistent_envelope_cannot_impersonate_another_call(tmp_path) -> None:
    import sqlite3

    db, store = _store(tmp_path)
    # A perfectly valid snapshot — for a different role.
    other = freeze_model_input(_request(), _context(role="planner", purpose="plan"))
    other_id = _insert(db, store, other, snapshot_id="snap-planner")

    # The store will not hand a snapshot of a different call to this one.
    with db.transaction() as connection:
        with pytest.raises(SnapshotBindingConflict):
            store.require_bindable(
                connection, other_id,
                owner_id="owner-a", runtime_bundle_id="bundle-1",
                role="conversation", purpose="route_and_respond",
            )

    _invocation(db, invocation_id="inv-conversation")
    # Forced past the store, the database refuses the binding outright: an
    # existing call can never be given an input after the fact, so a mismatch
    # cannot be recorded in the first place.
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "UPDATE model_invocations SET context_snapshot_id=?, context_snapshot_digest=? "
                "WHERE id='inv-conversation'",
                (other_id, other.content_digest),
            )


def test_p04_an_unknown_envelope_version_is_refused(tmp_path) -> None:
    db, store = _store(tmp_path)
    envelope = json.loads(freeze_model_input(_request(), _context()).content_json)
    envelope["schema_version"] = "model-input-snapshot-v99"
    content = canonical_json(envelope)
    import hashlib

    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_input_snapshots"
            "(id,owner_id,schema_version,content_json,content_digest,created_at) VALUES (?,?,?,?,?,?)",
            (
                "snap-v99", "owner-a", "model-input-snapshot-v1", content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(), "now",
            ),
        )
    with pytest.raises(UnknownSnapshotVersion):
        store.load("owner-a", "snap-v99")


# --------------------------------------------------------------------------- #
# P06/P08: binding once, and reading only your own
# --------------------------------------------------------------------------- #

def test_p06_a_binding_is_written_once_and_can_never_be_attached_later(tmp_path) -> None:
    import sqlite3

    db, store = _store(tmp_path)
    snapshot = freeze_model_input(_request(), _context())
    first = _insert(db, store, snapshot, snapshot_id="snap-1")
    second = _insert(db, store, snapshot, snapshot_id="snap-2")

    # The binding is a column of the invocation's INSERT, not a follow-up write.
    _invocation(db, invocation_id="inv-open", snapshot_id=first, digest=snapshot.content_digest)
    assert store.load_for_invocation("owner-a", "inv-open").id == first
    # Two rows, same content: the binding records which one this call used.
    assert first != second

    # Replacing the pair or clearing it is refused by the database.
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "UPDATE model_invocations SET context_snapshot_id=? WHERE id='inv-open'", (second,),
            )
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "UPDATE model_invocations SET context_snapshot_id=NULL, context_snapshot_digest='' "
                "WHERE id='inv-open'",
            )
    assert store.load_for_invocation("owner-a", "inv-open").id == first

    # A call created without a snapshot stays that way.  Back-filling it would
    # fabricate evidence of an input nobody recorded, so it is refused too: the
    # call remains a legacy record rather than becoming a complete one.
    _invocation(db, invocation_id="inv-legacy", digest="legacy-digest")
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "UPDATE model_invocations SET context_snapshot_id=?, context_snapshot_digest=? "
                "WHERE id='inv-legacy'",
                (first, snapshot.content_digest),
            )
    with pytest.raises(SnapshotBindingMissing):
        store.load_for_invocation("owner-a", "inv-legacy")


def test_p06_reading_a_binding_for_an_unknown_call_or_snapshot_is_a_domain_error(tmp_path) -> None:
    db, store = _store(tmp_path)
    snapshot_id = _insert(db, store, freeze_model_input(_request(), _context()))

    with pytest.raises(SnapshotMissing):
        with db.transaction() as connection:
            store.require_bindable(
                connection, "snap-missing",
                owner_id="owner-a", runtime_bundle_id="bundle-1",
                role="conversation", purpose="route_and_respond",
            )
    assert store.load("owner-a", snapshot_id).id == snapshot_id
    with pytest.raises(ModelInvocationMissing):
        store.load_for_invocation("owner-a", "inv-missing")


def test_p08_identical_input_gets_independent_rows_and_cross_owner_reads_fail(tmp_path) -> None:
    db, store = _store(tmp_path)
    request = _request()
    owner_a = freeze_model_input(request, _context(owner_id="owner-a"))
    owner_b = freeze_model_input(request, _context(owner_id="owner-b"))

    a_id = _insert(db, store, owner_a, snapshot_id="snap-a")
    b_id = _insert(db, store, owner_b, snapshot_id="snap-b")
    # Same messages and tools, but the binding is part of the envelope, so the
    # digests differ; two calls with truly identical input would share one.
    assert a_id != b_id
    assert store.load("owner-a", a_id).owner_id == "owner-a"
    assert store.load("owner-b", b_id).owner_id == "owner-b"

    _invocation(
        db, invocation_id="inv-a", owner_id="owner-a",
        snapshot_id=a_id, digest=owner_a.content_digest,
    )
    _invocation(
        db, invocation_id="inv-b", owner_id="owner-b",
        snapshot_id=b_id, digest=owner_b.content_digest,
    )

    # Cross-owner reads are missing, and the error carries no content.
    with pytest.raises(SnapshotMissing) as excinfo:
        store.load("owner-b", a_id)
    assert "第一问" not in str(excinfo.value)
    with pytest.raises(SnapshotMissing):
        store.load("owner-a", b_id)
    # Another owner's call is not even visible as a call.
    with pytest.raises(ModelInvocationMissing):
        store.load_for_invocation("owner-b", "inv-a")
    with pytest.raises(ModelInvocationMissing):
        store.load_for_invocation("owner-a", "inv-b")


def test_p08_two_calls_with_the_same_input_share_a_digest_but_not_a_row(tmp_path) -> None:
    db, store = _store(tmp_path)
    snapshot = freeze_model_input(_request(), _context())
    first = _insert(db, store, snapshot, snapshot_id="snap-1")
    second = _insert(db, store, snapshot, snapshot_id="snap-2")

    _invocation(db, invocation_id="inv-1", snapshot_id=first, digest=snapshot.content_digest)
    _invocation(db, invocation_id="inv-2", snapshot_id=second, digest=snapshot.content_digest)

    one = store.load_for_invocation("owner-a", "inv-1")
    two = store.load_for_invocation("owner-a", "inv-2")
    assert one.id == "snap-1" and two.id == "snap-2"
    assert one.content_digest == two.content_digest == snapshot.content_digest
    with db.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0]
    assert count == 2


# --------------------------------------------------------------------------- #
# legacy classification
# --------------------------------------------------------------------------- #

def test_a_call_without_a_snapshot_is_legacy_rather_than_corrupt(tmp_path) -> None:
    db, store = _store(tmp_path)
    # The shape rule 3.5/6 describes: an old digest value with no snapshot id.
    _invocation(db, invocation_id="inv-legacy", digest="legacy-digest")

    with pytest.raises(SnapshotBindingMissing):
        store.load_for_invocation("owner-a", "inv-legacy")


def test_insert_accepts_a_caller_supplied_id_and_rejects_a_duplicate(tmp_path) -> None:
    import sqlite3

    db, store = _store(tmp_path)
    snapshot = freeze_model_input(_request(), _context())
    assert _insert(db, store, snapshot, snapshot_id="snap-fixed") == "snap-fixed"
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, store, snapshot, snapshot_id="snap-fixed")

    # Without an id the store mints one, and the snapshot itself is untouched.
    generated = _insert(db, store, snapshot)
    assert generated.startswith("model_input_snapshot_")
    assert snapshot.id is None
    assert isinstance(store.load("owner-a", generated), ModelInputSnapshot)


# --------------------------------------------------------------------------- #
# U07: the call-level binding (begin_invocation)
# --------------------------------------------------------------------------- #

def _profile():
    from app.model_gateway import ModelProfile

    return ModelProfile("https://provider.test/v1", "demo", "MODEL_KEY", max_attempts=1)


def _control(tmp_path):
    from app.model_control import ModelControlStore

    db = Database(tmp_path / "control.db")
    return db, ModelControlStore(db), ModelInputSnapshotStore(db)


def test_u07_a_snapshot_is_frozen_persisted_and_bound_in_one_transaction(tmp_path) -> None:
    db, control, store = _control(tmp_path)
    context = _context()
    snapshot = freeze_model_input(_request(), context)

    handle = control.begin_invocation(_profile(), _request(), context, snapshot=snapshot)

    assert handle.input_snapshot_id
    assert handle.context.input_snapshot_id == handle.input_snapshot_id
    assert handle.context.context_snapshot_digest == snapshot.content_digest
    bound = store.load_for_invocation("owner-a", handle.invocation_id)
    assert bound.content_digest == snapshot.content_digest
    assert bound.id == handle.input_snapshot_id
    with db.connection() as connection:
        assert connection.execute(
            "SELECT context_snapshot_id, context_snapshot_digest FROM model_invocations WHERE id=?",
            (handle.invocation_id,),
        ).fetchone()[1] == snapshot.content_digest


def test_u07_derived_digests_come_from_the_frozen_content(tmp_path) -> None:
    db, control, store = _control(tmp_path)
    context = _context()
    snapshot = freeze_model_input(_request(), context)
    # The caller's object is mutated after freezing; it must not reach the row.
    mutated = _request(messages=[{"role": "user", "content": "被改写"}])

    frozen_handle = control.begin_invocation(_profile(), _request(), context, snapshot=snapshot)
    mutated_handle = control.begin_invocation(_profile(), mutated, context, snapshot=snapshot)

    with db.connection() as connection:
        digests = [
            tuple(connection.execute(
                "SELECT request_digest, tool_schema_digest, system_prompt_digest "
                "FROM model_invocations WHERE id=?", (invocation_id,),
            ).fetchone())
            for invocation_id in (frozen_handle.invocation_id, mutated_handle.invocation_id)
        ]
    assert digests[0] == digests[1]
    assert digests[0][0] and digests[0][1] and digests[0][2]


def test_u07_a_pre_bound_snapshot_is_verified_and_never_trusted(tmp_path) -> None:
    from app.model_control import ModelControlStore

    db, control, store = _control(tmp_path)
    good = freeze_model_input(_request(), _context())
    good_id = _insert(db, store, good, snapshot_id="snap-good")
    other = freeze_model_input(_request(), _context(role="planner", purpose="plan"))
    other_id = _insert(db, store, other, snapshot_id="snap-other")

    # A pre-bound snapshot that describes a different call is refused.
    with pytest.raises(SnapshotBindingConflict):
        control.begin_invocation(_profile(), _request(), _context(input_snapshot_id=other_id))
    # A digest with nothing behind it is refused, never recorded.
    with pytest.raises(SnapshotBindingConflict):
        control.begin_invocation(_profile(), _request(), _context(context_snapshot_digest="0" * 64))
    # A digest that contradicts the snapshot it names is refused.
    with pytest.raises(SnapshotBindingConflict):
        control.begin_invocation(
            _profile(), _request(),
            _context(input_snapshot_id=good_id, context_snapshot_digest="0" * 64),
        )
    # An unknown id is refused rather than silently replaced by a new freeze.
    with pytest.raises(SnapshotMissing):
        control.begin_invocation(_profile(), _request(), _context(input_snapshot_id="snap-absent"))

    # The verified pair is accepted, and no second snapshot is created.
    handle = control.begin_invocation(_profile(), _request(), _context(input_snapshot_id=good_id))
    assert handle.input_snapshot_id == good_id
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 2


def test_u07_a_derived_call_clears_the_inherited_binding_but_a_retry_keeps_it(tmp_path) -> None:
    from app.model_control import child_call_context

    parent = _context(input_snapshot_id="snap-parent", context_snapshot_digest="a" * 64)

    # Same purpose: a role adaptation inside one invocation — the same logical
    # call, so it keeps the input it was already bound to.
    retry = child_call_context(parent, role="conversation", purpose="route_and_respond")
    assert retry.input_snapshot_id == "snap-parent"
    assert retry.context_snapshot_digest == "a" * 64

    # Nested purpose: a new logical call, so it must freeze its own input.
    derived = child_call_context(parent, role="conversation", purpose="summarize")
    assert derived.input_snapshot_id is None
    assert derived.context_snapshot_digest == ""


def test_u07_idempotent_replay_neither_resends_nor_duplicates_the_snapshot(tmp_path) -> None:
    from app.model_control import InvocationIdempotencyConflict, InvocationReplayError

    db, control, store = _control(tmp_path)
    context = _context(idempotency_key="same-key")
    snapshot = freeze_model_input(_request(), context)

    first = control.begin_invocation(_profile(), _request(), context, snapshot=snapshot)
    with pytest.raises(InvocationReplayError):
        control.begin_invocation(_profile(), _request(), context, snapshot=snapshot)

    # The replay is detected before anything is written, so the failed attempt
    # leaves neither a second snapshot nor a second invocation behind.
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 1
    assert store.load_for_invocation("owner-a", first.invocation_id).id == first.input_snapshot_id

    # The same key with different input is a conflict, not a silent overwrite.
    changed = freeze_model_input(_request(messages=[{"role": "user", "content": "另一问"}]), context)
    with pytest.raises(InvocationIdempotencyConflict):
        control.begin_invocation(_profile(), _request(), context, snapshot=changed)
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# K01-K04: what makes a re-used idempotency key a replay
#
# F03 (independent review, 2026-10-06): with no harness the execution digest is
# empty, and the comparison only ran when *both* sides had one.  A key re-used
# with a different run/thread/turn/task/budget was therefore answered as a
# replay of the first call.  These cases pin the contract: the persisted public
# identity fields are compared field by field, nulls included, and a one-sided
# harness is never "the same call".
# --------------------------------------------------------------------------- #

def _harness(**overrides):
    from app.execution_context import create_root_context

    values = {"owner_id": "owner-a", "runtime_bundle_id": "bundle-1"}
    values.update(overrides)
    return create_root_context(**values)


def _harness_context(harness, **overrides):
    values = {
        "role": "conversation", "purpose": "route_and_respond", "idempotency_key": "same-key",
    }
    values.update(overrides)
    return ModelCallContext.from_harness(harness, **values)


def test_k01_an_identical_call_is_still_a_replay_and_writes_nothing(tmp_path) -> None:
    from app.model_control import InvocationReplayError

    db, control, _store = _control(tmp_path)
    context = _context(idempotency_key="same-key")

    control.begin_invocation(_profile(), _request(), context)
    with pytest.raises(InvocationReplayError):
        control.begin_invocation(_profile(), _request(), context)

    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 1


# Every public identity column the ledger persists, in both directions: a value
# that appears where there was none, one that disappears, and one value replaced
# by another.  ``label`` is the name the conflict must report.
_K02_CASES = [
    ("run_id", {}, {"run_id": "run-B"}, "run"),
    ("run_id", {"run_id": "run-A"}, {"run_id": None}, "run"),
    ("thread_id", {}, {"thread_id": "thread-B"}, "thread"),
    ("thread_id", {"thread_id": "thread-A"}, {"thread_id": None}, "thread"),
    ("turn_id", {}, {"turn_id": "turn-B"}, "turn"),
    ("turn_id", {"turn_id": "turn-A"}, {"turn_id": None}, "turn"),
    ("agent_task_id", {}, {"agent_task_id": "task-B"}, "task"),
    ("agent_task_id", {"agent_task_id": "task-A"}, {"agent_task_id": None}, "task"),
    ("root_budget_id", {}, {"root_budget_id": "budget-B"}, "budget"),
    ("root_budget_id", {"root_budget_id": "budget-A"}, {"root_budget_id": None}, "budget"),
    ("runtime_bundle_id", {}, {"runtime_bundle_id": "bundle-B"}, "bundle"),
    ("runtime_bundle_id", {"runtime_bundle_id": "bundle-A"}, {"runtime_bundle_id": None}, "bundle"),
    ("owner_id", {"owner_id": "owner-a"}, {"owner_id": "owner-b"}, "owner"),
    ("purpose", {"purpose": "route_and_respond"}, {"purpose": "summarize"}, "purpose"),
]


@pytest.mark.parametrize(
    "field,first,second,label",
    _K02_CASES,
    ids=[f"{case[0]}-{'value' if case[2].get(case[0]) is not None else 'null'}" for case in _K02_CASES],
)
def test_k02_without_a_harness_every_public_identity_field_is_compared(
    tmp_path, field, first, second, label,
) -> None:
    """F03's original reproduction, generalised to every persisted identity field.

    No harness on either side, so nothing but the public columns can tell the two
    calls apart — and they must.
    """
    from app.model_control import InvocationIdempotencyConflict

    db, control, _store = _control(tmp_path)
    control.begin_invocation(
        _profile(), _request(), _context(idempotency_key="same-key", **first),
    )

    with pytest.raises(InvocationIdempotencyConflict) as error:
        control.begin_invocation(
            _profile(), _request(), _context(idempotency_key="same-key", **second),
        )
    assert label in str(error.value), str(error.value)

    # The refused call left nothing behind.
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 1


@pytest.mark.parametrize("harness_first", [True, False], ids=["harness-then-legacy", "legacy-then-harness"])
def test_k03_a_one_sided_harness_is_never_the_same_call(tmp_path, harness_first) -> None:
    """A call opened under an execution identity is not the legacy call with the
    same columns, and vice versa.  Either direction is a conflict."""
    from app.model_control import InvocationIdempotencyConflict

    db, control, _store = _control(tmp_path)
    with_harness = _harness_context(_harness())
    without = _context(idempotency_key="same-key")

    first, second = (with_harness, without) if harness_first else (without, with_harness)
    control.begin_invocation(_profile(), _request(), first)
    with pytest.raises(InvocationIdempotencyConflict):
        control.begin_invocation(_profile(), _request(), second)


def test_k04_the_same_columns_with_a_different_span_or_input_are_a_conflict(tmp_path) -> None:
    from app.model_control import InvocationIdempotencyConflict

    db, control, _store = _control(tmp_path)

    # Same owner/run/thread/turn/bundle, but a different trace and span.
    control.begin_invocation(_profile(), _request(), _harness_context(_harness()))
    with pytest.raises(InvocationIdempotencyConflict) as error:
        control.begin_invocation(_profile(), _request(), _harness_context(_harness()))
    assert "execution identity" in str(error.value), str(error.value)

    # The same key with different input is never a replay either.
    plain = _context(idempotency_key="input-key")
    control.begin_invocation(_profile(), _request(), plain)
    with pytest.raises(InvocationIdempotencyConflict):
        control.begin_invocation(
            _profile(), _request(messages=[{"role": "user", "content": "另一问"}]), plain,
        )
