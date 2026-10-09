"""Request-controlled offline flags cannot alter server gateway assembly."""
import pytest
from fastapi.testclient import TestClient

from test_api import _headers
from test_snapshot_flow import _plane
from test_snapshot_goal_runtime_entries import _runtime


@pytest.mark.asyncio
async def test_t24_included_reference_with_missing_location_refuses_send(tmp_path, monkeypatch):
    from app.model_input_snapshot import SnapshotError
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport
    from test_snapshot_flow import _drive
    from test_snapshot_gateway import _answer

    db, _, _, model, gateway = _plane(tmp_path, monkeypatch, execute=None)
    recorder = CommittedSnapshotTransport(db, "local-user", "CS-CA-01", _answer())
    gateway._execute_attempt = recorder
    with pytest.raises(SnapshotError):
        await _drive(model, context_sources={"memory":{"candidates":[{
            "kind":"revision","id":"known-revision","included":True,
            "location":{"message_index":999,"field":"content"}}]}})
    assert recorder.send_count == 0


@pytest.mark.asyncio
async def test_t24_known_reference_change_cannot_rebind_a_logical_call(tmp_path, monkeypatch):
    from app.model_input_snapshot import build_provenance, SnapshotBindingConflict
    from app.model_control import InvocationReplayError, InvocationIdempotencyConflict
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from test_snapshot_gateway import _context, _request, _answer
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport

    db, bundle, _, _, gateway = _plane(tmp_path, monkeypatch, execute=None)
    recorder = CommittedSnapshotTransport(db, "local-user", "CS-CA-01", _answer())
    gateway._execute_attempt = recorder
    context = _context(bundle, idempotency_key="known-reference")
    source = {"kind":"skill", "id":"known-skill", "version":"v1", "content_digest":"digest-v1", "included":True}
    await gateway.complete(_request(), context=context, provenance=build_provenance([source]))
    frozen = ModelInputSnapshotStore(db).load("local-user", recorder.observations[0]["snapshot_id"])
    with pytest.raises((SnapshotBindingConflict, InvocationReplayError, InvocationIdempotencyConflict)):
        await gateway.complete(_request(), context=context, provenance=build_provenance([{**source,"version":"v2","content_digest":"digest-v2"}]))
    assert recorder.send_count == 1
    assert ModelInputSnapshotStore(db).load("local-user", frozen.id).content_json == frozen.content_json


@pytest.mark.parametrize("location", ["body", "query"])
def test_t28_request_cannot_enable_offline_gateway(tmp_path, monkeypatch, location):
    from app.main import create_app
    from app.model_control import RoutingError

    calls = []

    async def execute(profile, request, **kwargs):
        calls.append(request)
        raise AssertionError("identity-less request must never reach provider")

    db, _, _, _, gateway = _plane(tmp_path, monkeypatch, execute=execute)
    runtime = _runtime(tmp_path, db, gateway)
    app = create_app(runtime=runtime)
    client = TestClient(app)
    payload = {"title": "Goal", "description": "Goal"}
    query = "?offline_unbound=true&owner_id=attacker" if location == "query" else ""
    if location == "body":
        payload.update(offline_unbound=True, owner_id="attacker")
    response = client.post("/api/goals" + query, json=payload, headers=_headers(app))
    assert response.status_code == 201, response.text
    assert runtime.owner_id == "local-user"
    assert gateway.control_store is not None
    import asyncio
    from app.model_gateway import ModelRequest
    with pytest.raises(RoutingError, match="owner is required"):
        asyncio.run(gateway.complete(ModelRequest(messages=[], purpose="probe")))
    assert calls == []


def test_t32_static_candidates_have_explicit_dispositions():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts/audit_snapshot_callsites.py"
    spec = importlib.util.spec_from_file_location("snapshot_audit", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Scan the actual tree: all call candidates must have an explicit disposition.
    rows = module.candidates()
    assert rows
    assert all(row["excluded_reason"] or row["callsites"] for row in rows)
    assert {"CS-EV-04", "CS-CA-02", "CS-GP-03"} <= {site for row in rows for site in row["callsites"]}
    assembly = module.assembly_candidates()
    assert assembly and all(row["disposition"] for row in assembly)
    assert all(row["keywords"].get("offline_unbound") in (None, "True") for row in assembly)
    assert not any(row["keywords"].get("offline_unbound") for row in assembly if not row["file"].endswith("/eval.py"))


@pytest.mark.asyncio
async def test_t28_tool_parameters_cannot_enable_offline(tmp_path):
    from test_goal_tools import _tool_runtime, _context
    from app.tools import ToolCall, ToolRejected

    runtime = _tool_runtime(tmp_path)
    thread = runtime.conversation.create_thread("Offline injection")
    with pytest.raises(ToolRejected, match="unknown tool parameter"):
        await runtime.tools.execute_async(
            ToolCall("injection", "query_goals", {"offline_unbound": True}),
            context=_context(runtime, thread.id), skill_tools=None,
        )


@pytest.mark.asyncio
async def test_t08_unfinished_invocation_replay_never_resends(tmp_path, monkeypatch):
    from app.model_control import InvocationReplayError
    from test_snapshot_gateway import _context, _request, _answer
    from app.model_input_snapshot_store import ModelInputSnapshotStore

    sent = []

    async def execute(profile, request, **kwargs):
        sent.append(request)
        return _answer()

    db, bundle, _, _, gateway = _plane(tmp_path, monkeypatch, execute=execute)
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport
    gateway._execute_attempt = CommittedSnapshotTransport(db, "local-user", "CS-CA-01", _answer())
    context = _context(bundle, idempotency_key="unknown-call")
    await gateway.complete(_request(), context=context)
    with db.transaction() as connection:
        row = connection.execute("SELECT id,context_snapshot_id FROM model_invocations").fetchone()
        connection.execute("UPDATE model_invocations SET status='RUNNING' WHERE id=?", (row["id"],))
    frozen = ModelInputSnapshotStore(db).load("local-user", row["context_snapshot_id"])
    with pytest.raises(InvocationReplayError):
        await gateway.complete(_request(), context=context)
    assert gateway._execute_attempt.send_count == 1
    assert ModelInputSnapshotStore(db).load("local-user", frozen.id).content_json == frozen.content_json
