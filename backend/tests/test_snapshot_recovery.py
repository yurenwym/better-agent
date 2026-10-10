"""A01-A08: recovery, revocation and audit around the frozen input.

These cases reuse the production approval chain from the first phase
(``ManagedTurnWorker`` -> ``ChatToolRunner`` -> ``ToolRegistry`` ->
``GoalProgramService``) and the routed gateway harness from the gateway cases.
The point of each one is that the frozen input is a *fact about the past*: a
restart, a revocation or a crash can stop a call, but none of them may rewrite,
back-fill or silently replace what a call was given.
"""
from __future__ import annotations

import json

import pytest

from test_harness_context_approval import (
    _approval_runtime,
    _identity,
    _pause_on_write,
    _planner_rows,
    _preview_script,
    _tool_row,
)
from test_harness_context_flow import reopen_runtime, stored_context
from test_snapshot_flow import _context, _echo_answer, _invocations, _plane, _request


def _snapshot_store(db):
    from app.model_input_snapshot_store import ModelInputSnapshotStore

    return ModelInputSnapshotStore(db)


# --------------------------------------------------------------------------- #
# A01: an approval resume keeps the tool's identity and freezes a new call
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a01_a_resumed_approval_keeps_the_tool_identity_and_freezes_its_own_call(
    tmp_path, monkeypatch,
) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)

    original_identity = _identity(runtime, pending.id)
    original_tool = stored_context(_tool_row(runtime, pending.id))

    # The pause itself already made a conversation call, and it is frozen.
    with runtime.db.connection() as connection:
        conversation_calls = connection.execute(
            "SELECT id, context_snapshot_id FROM model_invocations WHERE role='conversation'"
        ).fetchall()
    assert conversation_calls
    assert all(row["context_snapshot_id"] for row in conversation_calls)

    reopened = reopen_runtime(tmp_path, script)
    continuation = reopened.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a01-approve",
    )
    assert await reopened.turn_worker.run_once() is True
    assert reopened.conversation.turn(continuation.id).status == "COMPLETED"

    # The tool's identity survives the restart byte for byte.
    assert _identity(reopened, pending.id) == original_identity
    assert stored_context(_tool_row(reopened, pending.id)) == original_tool

    # The compile inside the tool is a new logical call with its own frozen
    # input, rooted in the tool's span rather than the continuation's.
    planners = _planner_rows(reopened)
    assert len(planners) == 1
    planner = planners[0]
    assert planner["context_snapshot_id"]
    snapshot = _snapshot_store(reopened.db).load_for_invocation(
        original_tool["owner_id"], planner["id"],
    )
    assert snapshot.id == planner["context_snapshot_id"]
    assert snapshot.content_digest == planner["context_snapshot_digest"]
    assert (snapshot.role, snapshot.purpose) == ("planner", "compile_goal_program")
    assert snapshot.runtime_bundle_id == original_tool["runtime_bundle_id"]

    planner_context = stored_context(planner)
    from app.event_envelope import EventMetadata
    resumed = next(event for event in reopened.conversation.events.list(thread.id)
                   if event.type == "chat_tool.context_resumed")
    attempt = EventMetadata.from_dict(json.loads(resumed.envelope_json)).context
    assert attempt.parent_span_id == original_tool["span_id"]
    assert planner_context["parent_span_id"] == attempt.span_id
    assert planner_context["trace_id"] == original_tool["trace_id"]
    assert planner_context["span_id"] != original_tool["span_id"]


# --------------------------------------------------------------------------- #
# A02: a tool revoked while the turn waits cannot be resumed into a new call
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a02_a_revoked_tool_resumes_into_no_call_and_no_new_snapshot(
    tmp_path, monkeypatch,
) -> None:
    script = _preview_script()
    runtime, thread = _approval_runtime(tmp_path, monkeypatch, script)
    accepted, paused, pending = await _pause_on_write(runtime, thread)
    before = _identity(runtime, pending.id)

    # The turn's allowance no longer names the tool: the current permission set
    # decides, and restoring a context is not restoring an authorization.
    runtime.conversation_tool_allowance = lambda turn_id, skill_names: {"get_today_tasks"}
    runtime.conversation.decide_tool_call(
        accepted.turn_id, "approve", paused.version, "a02-approve",
    )
    await runtime.turn_worker.run_once()

    stored = runtime.conversation.chat_tool_calls.get(pending.id)
    assert stored.status == "FAILED" and stored.error_code == "TOOL_NOT_ALLOWED"
    # The tool never ran, so the compile inside it never happened.
    assert _planner_rows(runtime) == []
    # The continuation turn still talks to the model - that is a conversation
    # call, not the revoked tool's - and every call that was made is frozen.
    with runtime.db.connection() as connection:
        rows = connection.execute(
            "SELECT role, context_snapshot_id FROM model_invocations"
        ).fetchall()
    assert rows
    assert all(row["context_snapshot_id"] for row in rows)
    assert "planner" not in {row["role"] for row in rows}
    assert _identity(runtime, pending.id) == before


# --------------------------------------------------------------------------- #
# A03: a committed call stays readable when its response never lands
# --------------------------------------------------------------------------- #

def test_a03_a_committed_call_is_readable_after_the_process_disappears(
    tmp_path, monkeypatch,
) -> None:
    from app.db import Database
    from app.model_control import ModelControlStore, open_model_invocation
    from test_snapshot_gateway import _registered_profile

    db, bundle, versions, _model, _gateway = _plane(
        tmp_path, monkeypatch, execute=_echo_answer(),
    )
    profile = _registered_profile(db, versions["chat"])
    control = ModelControlStore(db)
    request = _request([{"role": "user", "content": "第一问"}])

    handle, _snapshot, _frozen = open_model_invocation(
        control, profile, request, _context(bundle),
    )

    # The process dies here: no attempt was ever started, no response stored.
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM model_invocations WHERE id=?", (handle.invocation_id,),
        ).fetchone()["status"] == "RUNNING"

    # A brand-new handle on the same database still recovers the input: it was
    # committed before anything was sent, which is the whole point.
    reopened = Database(db.path)
    stored = _snapshot_store(reopened).load_for_invocation(
        "local-user", handle.invocation_id,
    )
    assert stored.id == handle.input_snapshot_id
    assert stored.to_request().messages == [{"role": "user", "content": "第一问"}]
    # Having the input is not permission to replay a side effect.
    with reopened.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 1
    reopened.close()


# --------------------------------------------------------------------------- #
# A04: a call that never had an input stays legacy, and is never back-filled
# --------------------------------------------------------------------------- #

def test_a04_a_call_without_a_snapshot_stays_legacy_and_cannot_be_backfilled(tmp_path) -> None:
    import sqlite3

    from app.db import Database
    from app.model_input_snapshot_store import SnapshotBindingMissing
    from test_model_input_snapshot_store import _invocation

    db = Database(tmp_path / "legacy.db")
    store = _snapshot_store(db)
    # The shape rule 3.5/6 describes: an old digest value with no snapshot id.
    _invocation(db, invocation_id="inv-legacy", digest="legacy-digest")

    with pytest.raises(SnapshotBindingMissing):
        store.load_for_invocation("owner-a", "inv-legacy")

    # Repairing it afterwards would fabricate a record of what it sent, so the
    # database refuses the write rather than the reader having to distrust it.
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as connection:
            connection.execute(
                "UPDATE model_invocations SET context_snapshot_id='snap-late' "
                "WHERE id='inv-legacy'"
            )


# --------------------------------------------------------------------------- #
# A05/A06: an asset revoked after the freeze stops the send
#
# Both gateways are driven through their real send path and the network is
# counted *outside* the gateway, so the assertions are about bytes that did or
# did not leave - not about a check helper being called.
# --------------------------------------------------------------------------- #

def _learning_assets(db, tmp_path):
    from app.events import EventStore
    from app.learning import LearningService
    from app.memory import MemoryService

    learning = LearningService(db, MemoryService(db, EventStore(db), tmp_path / "memory"), None)
    return learning.assets


def _revocation_gateway(db, versions, control, *, kind, handler, execute):
    """The chosen gateway, with its network scripted by ``handler``/``execute``."""
    from dataclasses import replace

    import httpx

    from app.model_control import RoutedModelGateway
    from app.model_gateway import ModelGateway
    from test_snapshot_gateway import _registered_profile

    if kind == "direct":
        profile = replace(_registered_profile(db, versions["chat"]), retry_base_seconds=0)
        return ModelGateway(
            profile, transport=httpx.MockTransport(handler), control_store=control,
        )
    return RoutedModelGateway(db, control, execute_attempt=execute)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "routed"])
async def test_a05_an_asset_revoked_after_the_freeze_is_refused_before_the_send(
    tmp_path, monkeypatch, kind,
) -> None:
    """The input is frozen and bound, then the asset is revoked before the wire.

    Zero network requests, the invocation ended FAILED, and the frozen input is
    exactly what it was - a refusal is not a repair.
    """
    import httpx

    from app.learning_assets import LearningConflict
    from app.model_control import ModelControlStore
    from test_snapshot_gateway import (
        _SSE_OK, _configured_control_plane, _invocation_row, _only_invocation_id,
    )

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    assets = _learning_assets(db, tmp_path)
    control = ModelControlStore(db)
    control.learning_assets = assets
    wire: list = []
    revoked: list = []

    def handler(request):
        wire.append(request)
        return httpx.Response(200, content=_SSE_OK)

    async def execute(profile, request, **_):
        from test_snapshot_gateway import _answer

        wire.append(request)
        return _answer()

    def on_attempt_started(ordinal, reason):
        # The last thing a caller controls before the send.
        if not revoked:
            assets.revoke("local-user", "bundle", bundle.id, "revoked_after_freeze")
            revoked.append(True)

    gateway = _revocation_gateway(
        db, versions, control, kind=kind, handler=handler, execute=execute,
    )
    with pytest.raises(LearningConflict):
        await gateway.complete(
            _request([{"role": "user", "content": "第一问"}]), context=_context(bundle),
            on_attempt_started=on_attempt_started,
        )

    assert revoked, "the revocation callback never ran"
    assert wire == [], f"{kind}: a revoked asset must stop the send before the network"
    invocation_id = _only_invocation_id(db)
    row = _invocation_row(db, invocation_id)
    assert row["status"] == "FAILED"
    stored = _snapshot_store(db).load_for_invocation("local-user", invocation_id)
    assert stored.id == row["context_snapshot_id"]
    assert stored.content_digest == row["context_snapshot_digest"]
    assert stored.to_request().messages[-1] == {"role": "user", "content": "第一问"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "routed"])
async def test_a06_an_asset_revoked_between_attempts_stops_the_retry(
    tmp_path, monkeypatch, kind,
) -> None:
    """The first attempt fails and revokes; the retry must never reach the wire."""
    import httpx

    from app.learning_assets import LearningConflict
    from app.model_control import ModelControlStore
    from app.model_gateway import GatewayError
    from test_snapshot_gateway import (
        _configured_control_plane, _invocation_row, _only_invocation_id,
    )

    db, bundle, versions = _configured_control_plane(
        tmp_path, monkeypatch, max_attempts={"chat": 2},
    )
    assets = _learning_assets(db, tmp_path)
    control = ModelControlStore(db)
    control.learning_assets = assets
    wire: list = []

    def handler(request):
        wire.append(request)
        assets.revoke("local-user", "bundle", bundle.id, "revoked_between_attempts")
        return httpx.Response(504, json={"error": "upstream"})

    async def execute(profile, request, **_):
        wire.append(request)
        assets.revoke("local-user", "bundle", bundle.id, "revoked_between_attempts")
        raise GatewayError("temporary", "timeout")

    gateway = _revocation_gateway(
        db, versions, control, kind=kind, handler=handler, execute=execute,
    )
    with pytest.raises(LearningConflict):
        await gateway.complete(
            _request([{"role": "user", "content": "第一问"}]), context=_context(bundle),
        )

    assert len(wire) == 1, f"{kind}: the retry must be stopped by the revocation, not sent"
    assert _invocation_row(db, _only_invocation_id(db))["status"] == "FAILED"


# --------------------------------------------------------------------------- #
# A07: owner scoping, reference-only events and no credentials in the snapshot
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a07_snapshots_are_owner_scoped_and_leak_neither_text_nor_credentials(
    tmp_path, monkeypatch,
) -> None:
    from app.model_input_snapshot_store import (
        ModelInvocationMissing,
        SnapshotMissing,
    )

    db, bundle, _versions, _model, gateway = _plane(tmp_path, monkeypatch, execute=_echo_answer())
    await gateway.complete(_request([{"role": "user", "content": "第一问"}]), context=_context(bundle))

    rows = _invocations(db)
    invocation_id = rows[0]["id"]
    snapshot_id = rows[0]["context_snapshot_id"]
    store = _snapshot_store(db)
    stored = store.load("local-user", snapshot_id)

    # The reference-level view carries ids, digests and counts - never user text.
    assert "第一问" not in json.dumps(stored.public_view(), ensure_ascii=False)

    # Another owner cannot read the snapshot or even see the call.
    with pytest.raises(SnapshotMissing):
        store.load("someone-else", snapshot_id)
    with pytest.raises(ModelInvocationMissing):
        store.load_for_invocation("someone-else", invocation_id)

    # The credential used to reach the provider is not part of the input.
    assert "secret" not in stored.content_json

    # Ordinary events reference the call; they do not copy the user's text.
    with db.connection() as connection:
        events = connection.execute(
            "SELECT event_type, data_json FROM model_invocation_events"
        ).fetchall()
    assert events
    for event in events:
        assert "第一问" not in event["data_json"]


# --------------------------------------------------------------------------- #
# A08: a failing asset step sends nothing and leaves nothing behind
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a08_a_failing_asset_step_rolls_back_and_sends_nothing(tmp_path, monkeypatch) -> None:
    sent: list = []

    async def execute(profile, request, **_):
        sent.append(request)
        from test_snapshot_gateway import _answer

        return _answer()

    db, bundle, _versions, _model, gateway = _plane(tmp_path, monkeypatch, execute=execute)

    class _BrokenAssets:
        def freeze_request(self, connection, context, request, invocation_id):
            raise RuntimeError("asset step failed")

        def assert_request_active(self, invocation_id, owner_id):
            raise AssertionError("must never be reached")

    gateway.control_store.learning_assets = _BrokenAssets()

    with pytest.raises(RuntimeError):
        await gateway.complete(_request([{"role": "user", "content": "第一问"}]), context=_context(bundle))

    # The failure happened inside the creating transaction, so the snapshot and
    # the invocation rolled back with it and no request went out.
    assert sent == []
    assert _invocations(db) == []
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 0
