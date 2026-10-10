import json
from dataclasses import replace

import pytest

from app.event_envelope import EventMetadata
from app.event_projection import project_envelope
from app.execution_context import create_root_context
from app.execution_outcome import ExecutionOutcome, Scope, Status


def test_metadata_roundtrip_and_version():
    context = create_root_context(owner_id="owner", run_id="run")
    metadata = EventMetadata(context, "runtime", "run", outcome=ExecutionOutcome(Scope.LOOP, Status.COMPLETED))
    assert EventMetadata.from_dict(json.loads(json.dumps(metadata.to_dict()))) == metadata
    invalid = metadata.to_dict()
    invalid["schema_version"] = 2
    with pytest.raises(ValueError):
        EventMetadata.from_dict(invalid)


@pytest.mark.parametrize("terminal,expected", [("cancel", Status.CANCELLED), ("partial", Status.FAILED), ("retry", Status.FAILED)])
def test_research_lifecycle_outcomes_keep_partial_and_retry_distinct(tmp_path, terminal, expected):
    from test_research_service import build
    _, conversation, service = build(tmp_path)
    thread = conversation.create_thread("outcome")
    job = service.create_manual(thread.id, "topic", "one", ("web",))
    if terminal == "cancel":
        service.cancel(job.id)
    else:
        lease_1 = service.claim(job.id, "worker", 30)
        if terminal == "partial":
            service.complete_partial(job.id, "worker", "report", "partial report", 0, 0, (), ("missing",), epoch=lease_1.lease_epoch)
        else:
            service.fail(job.id, "worker", "timeout", retryable=True, epoch=lease_1.lease_epoch)
    kinds = {"cancel": "research.cancelled", "partial": "research.partial", "retry": "research.retry_scheduled"}
    event = next(event for event in conversation.events.list(thread.id) if event.type == kinds[terminal])
    outcome = EventMetadata.from_dict(json.loads(event.envelope_json)).outcome
    assert outcome.status == expected
    assert outcome.scope == (Scope.LOOP if terminal == "retry" else Scope.TASK)
    if terminal == "partial":
        assert outcome.error.code == "TASK_INCOMPLETE"
        assert service.get(job.id).status == "PARTIAL"
    if terminal == "retry":
        assert outcome.error.retryable and service.get(job.id).status == "QUEUED"


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "model_failed"])
def test_expert_failure_and_cancel_return_to_source_trace(tmp_path, terminal):
    from app.agents import AgentTaskService
    from app.behavior import BehaviorBundleService
    from app.conversation import ConversationService
    from app.db import Database
    db = Database(tmp_path / "expert.db")
    bundle = BehaviorBundleService(db).ensure({"code": "test"})
    BehaviorBundleService(db).activate("stable", bundle.id, "one")
    conversation = ConversationService(db)
    thread = conversation.create_thread("expert")
    source = conversation.accept_turn(thread.id, "one", "expert", [])
    tasks = AgentTaskService(db, thread_events=conversation.events)
    run = tasks.create_run("local-user", "topic", {}, bundle.id, thread_id=thread.id,
        parent_turn_id=source.turn_id, idempotency_key="one", append_thread_message=False)
    if terminal == "cancelled":
        tasks.cancel_run(run["id"], "cancel")
    else:
        task = tasks.claim_next("worker", 30)
        from app.model_gateway import GatewayError
        from app.outcome_adapters import error_from_exception
        error = error_from_exception(GatewayError("private-provider-detail", "budget")) if terminal == "model_failed" else None
        tasks.fail(task["id"], "worker", task["lease_epoch"], "MODEL_ERROR", retryable=False, error=error)
        if error:
            terminal = "failed"
            with db.connection() as connection:
                stored = connection.execute("SELECT error_json FROM agent_task_attempts WHERE task_id=?", (task["id"],)).fetchone()
            assert json.loads(stored["error_json"])["outcome"]["error"]["code"] == "MODEL_BUDGET"
            assert "private-provider-detail" not in stored["error_json"]
    events = conversation.events.list(thread.id)
    queued = next(event for event in events if event.type == "expert.run.queued")
    finished = next(event for event in events if event.type == f"expert.run.{terminal}")
    before, after = (EventMetadata.from_dict(json.loads(event.envelope_json)) for event in (queued, finished))
    assert after.context.trace_id == before.context.trace_id
    assert after.causation_event_id == queued.event_id
    assert after.outcome.scope == Scope.TASK and after.outcome.status == terminal
    if terminal == "failed":
        assert after.outcome.error.code == ("MODEL_BUDGET" if error else "INTERNAL_ERROR")


def test_projection_is_owner_checked_and_redacted():
    from app.events import Event
    metadata = EventMetadata(create_root_context(owner_id="owner", run_id="run"), "runtime", "run")
    event = Event(1, "id", 1, "run", "goal", "custom", "2026-10-09T00:00:00Z", "runtime", {},
                  {"api_key": "secret", "content": "private"}, json.dumps(metadata.to_dict()))
    with pytest.raises(PermissionError):
        project_envelope(event, owner_id="other")
    projected = project_envelope(event, owner_id="owner")
    assert "secret" not in json.dumps(projected)
    assert "private" not in json.dumps(projected)
    assert projected["stream_id"] == "run"
    with pytest.raises(PermissionError):
        project_envelope(replace(event, envelope_json=None), owner_id="owner")
    from app.event_projection import project_authorized_event
    assert project_authorized_event(replace(event, envelope_json=None), owner_id="owner")["compatibility"] == "legacy_context_missing"
    unknown = replace(event, envelope_json='{"schema_version":99,"secret":"private"}')
    assert project_authorized_event(unknown, owner_id="owner")["compatibility"] == "unsupported_version"


def test_store_idempotence_conflict_and_rollback(tmp_path):
    from app.db import Database
    from app.events import EventStore
    db = Database(tmp_path / "agent.db")
    store = EventStore(db)
    first = store.append("run", "goal", "custom", "runtime", {"x": 1}, event_id="same")
    assert store.append("run", "goal", "custom", "runtime", {"x": 1}, event_id="same") == first
    with pytest.raises(ValueError):
        store.append("run", "goal", "custom", "runtime", {"x": 2}, event_id="same")
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            store.append("run", "goal", "custom", "runtime", {}, connection=connection)
            raise RuntimeError("rollback")
    assert len(store.list("run")) == 1


@pytest.mark.asyncio
async def test_native_goal_persists_unified_result_and_trace(tmp_path, monkeypatch):
    from test_runtime import make_runtime
    from app.runtime import MockModelGateway, ModelDecision
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = make_runtime(tmp_path, MockModelGateway(plan_steps=[{"id": "s", "title": "step"}],
                                                     decisions=[ModelDecision.complete("done")]))
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    await runtime.approve_plan(run.id, 1)
    outcome = ExecutionOutcome.from_dict(runtime.get_run(run.id).budget["last_execution_outcome"])
    assert outcome.status == Status.COMPLETED and outcome.scope == Scope.LOOP
    events = [e for e in runtime.events.list(run.id) if e.envelope_json]
    assert events
    assert events[0].type == "run.created"
    metadata = [EventMetadata.from_dict(json.loads(e.envelope_json)) for e in events]
    assert len({m.context.trace_id for m in metadata}) == 1
    assert all(m.context.run_id == run.id for m in metadata)


@pytest.mark.asyncio
async def test_state_and_approval_rollback_when_event_cannot_commit(tmp_path, monkeypatch):
    from test_runtime import make_runtime
    from app.runtime import MockModelGateway, ModelDecision
    from app.tools import ToolCall
    from app.domain import AgentState
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = make_runtime(tmp_path, MockModelGateway(plan_steps=[{"id": "s", "title": "write"}],
        decisions=[ModelDecision.tool(ToolCall("write", "write_note", {"path": "once.md", "content": "once"}))]))
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    await runtime.approve_plan(run.id, 1)
    approval = runtime.pending_approvals(run.id)[0]
    before = runtime.get_run(run.id).state
    def fail(*args, **kwargs):
        raise RuntimeError("event unavailable")
    monkeypatch.setattr(runtime.events, "append", fail)
    with pytest.raises(RuntimeError, match="event unavailable"):
        await runtime.grant_approval(approval.id)
    assert runtime.pending_approvals(run.id)[0].id == approval.id
    assert not (tmp_path / "workspace/once.md").exists()
    with pytest.raises(RuntimeError, match="event unavailable"):
        runtime._transition(runtime.get_run(run.id), AgentState.CANCELLED, {})
    assert runtime.get_run(run.id).state == before


def test_envelope_api_keeps_old_shape_and_refuses_foreign_thread(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime
    runtime = make_runtime(tmp_path, MockModelGateway())
    client = TestClient(create_app(runtime=runtime))
    thread = runtime.conversation.create_thread("local")
    turn = runtime.conversation.accept_turn(thread.id, "one", "hello", [])
    runtime.conversation.harness_context.load_or_create_turn_context(turn.turn_id, owner_id="local-user", thread_id=thread.id)
    runtime.conversation.events.append(thread.id, turn.turn_id, "custom", "runtime", {"authorization": "secret"})
    headers = {"host": "127.0.0.1:8000"}
    old = client.get(f"/api/threads/{thread.id}/events", headers=headers).json()
    assert "envelope_json" not in str(old)
    projected = client.get(f"/api/threads/{thread.id}/events?envelope=true", headers=headers)
    assert projected.status_code == 200
    assert "secret" not in projected.text
    accepted = next(e for e in projected.json()["events"] if e["type"] == "turn.accepted")
    assert accepted["context"]["context"]["turn_id"] == turn.turn_id
    foreign = runtime.conversation.create_thread("private", owner_id="other")
    assert client.get(f"/api/threads/{foreign.id}/events?envelope=true", headers=headers).status_code == 404


def test_acceptance_identity_and_event_roll_back_together(tmp_path, monkeypatch):
    from app.conversation import ConversationService
    from app.db import Database
    service = ConversationService(Database(tmp_path / "entry.db"))
    thread = service.create_thread("entry")
    append = service.events.append

    def fail(*args, **kwargs):
        if args[2] == "turn.accepted":
            raise RuntimeError("event unavailable")
        return append(*args, **kwargs)

    monkeypatch.setattr(service.events, "append", fail)
    with pytest.raises(RuntimeError, match="event unavailable"):
        service.accept_turn(thread.id, "one", "hello", [])
    with service.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM thread_events").fetchone()[0] == 0
    monkeypatch.setattr(service.events, "append", append)
    turn = service.accept_turn(thread.id, "one", "hello", [])
    root = service.harness_context.load_turn_context(turn.turn_id)
    event = service.events.list(thread.id)[0]
    assert EventMetadata.from_dict(json.loads(event.envelope_json)).context == root
    assert service.accept_turn(thread.id, "one", "hello", []).turn_id == turn.turn_id
    assert len(service.events.list(thread.id)) == 1


def test_half_missing_context_cannot_be_written_as_legacy(tmp_path):
    from app.conversation import ConversationService
    from app.db import Database
    from app.execution_context import HarnessContextError
    service = ConversationService(Database(tmp_path / "corrupt.db"))
    thread = service.create_thread("entry")
    turn = service.accept_turn(thread.id, "one", "hello", [])
    with service.db.transaction() as connection:
        connection.execute("UPDATE turns SET execution_context_json=NULL WHERE id=?", (turn.turn_id,))
    with pytest.raises(HarnessContextError):
        service.events.append(thread.id, turn.turn_id, "turn.completed", "worker", {})
    assert len(service.events.list(thread.id)) == 1


def test_removed_root_cannot_be_regenerated(tmp_path):
    from app.conversation import ConversationService
    from app.db import Database
    from app.harness_context_store import ContextStoreConflict
    service = ConversationService(Database(tmp_path / "removed.db"))
    thread = service.create_thread("entry")
    turn = service.accept_turn(thread.id, "one", "hello", [])
    with service.db.transaction() as connection:
        connection.execute("UPDATE turns SET execution_context_json=NULL,execution_context_digest=NULL WHERE id=?", (turn.turn_id,))
    with pytest.raises(ContextStoreConflict, match="removed"):
        service.harness_context.load_or_create_turn_context(
            turn.turn_id, owner_id="local-user", thread_id=thread.id,
        )
    with service.db.connection() as connection:
        assert connection.execute("SELECT execution_context_json FROM turns WHERE id=?", (turn.turn_id,)).fetchone()[0] is None


@pytest.mark.parametrize("field", ["root_budget_id", "runtime_bundle_id", "project_id"])
def test_explicit_event_metadata_cannot_override_durable_bindings(tmp_path, field):
    from app.conversation import ConversationService
    from app.db import Database
    from app.execution_context import create_child_context
    service = ConversationService(Database(tmp_path / "bindings.db"))
    thread = service.create_thread("entry")
    turn = service.accept_turn(thread.id, "one", "hello", [])
    root = service.harness_context.load_turn_context(turn.turn_id)
    context = replace(create_child_context(root), **{field: "forged"})
    with pytest.raises(ValueError, match="identity conflict"):
        service.events.append(thread.id, turn.turn_id, "custom", "worker", {},
                              metadata=EventMetadata(context, "test", turn.turn_id))
    assert len(service.events.list(thread.id)) == 1


@pytest.mark.asyncio
async def test_run_event_rejects_budget_binding_drift(tmp_path, monkeypatch):
    from test_runtime import make_runtime
    from app.runtime import MockModelGateway
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = make_runtime(tmp_path, MockModelGateway())
    run = await runtime.create_goal("goal", "goal")
    before = len(runtime.events.list(run.id))
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE runs SET root_budget_id='foreign-budget' WHERE id=?", (run.id,))
    with pytest.raises(ValueError, match="budget binding"):
        runtime.events.append(run.id, run.goal_id, "custom", "worker", {})
    assert len(runtime.events.list(run.id)) == before


@pytest.mark.asyncio
async def test_checkpoint_observes_events_in_its_own_transaction(tmp_path):
    from test_runtime import make_runtime
    from app.runtime import MockModelGateway
    runtime = make_runtime(tmp_path, MockModelGateway())
    run = await runtime.create_goal("goal", "goal")
    with runtime.db.transaction() as connection:
        event = runtime.events.append(run.id, run.goal_id, "approval.requested", "runtime", {}, connection=connection)
        checkpoint = runtime._save_checkpoint(run.id, "waiting", connection=connection)
        assert checkpoint.last_event_seq == event.seq
    assert runtime.checkpoints.latest(run.id).last_event_seq == event.seq


def test_research_retry_uses_new_span_and_terminal_retains_attempt(tmp_path):
    from test_research_service import build
    _, conversation, service = build(tmp_path)
    thread = conversation.create_thread("retry")
    job = service.create_manual(thread.id, "topic", "one", ("web",))
    lease_1 = service.claim(job.id, "worker", 30)
    service.fail(job.id, "worker", "timeout", retryable=True, epoch=lease_1.lease_epoch)
    with service.db.transaction() as connection:
        connection.execute("UPDATE research_jobs SET available_at='2000-01-01T00:00:00+00:00' WHERE id=?", (job.id,))
    lease_2 = service.claim(job.id, "worker", 30)
    assert lease_2
    service.complete(job.id, "worker", "title", "report", 0, 0, epoch=lease_2.lease_epoch)
    events = [event for event in conversation.events.list(thread.id)
              if event.type in {"research.started", "research.retry_scheduled", "research.completed"}]
    metadata = [EventMetadata.from_dict(json.loads(event.envelope_json)) for event in events]
    assert [item.attempt for item in metadata] == [1, 1, 2, 2]
    assert metadata[0].context.span_id == metadata[1].context.span_id
    assert metadata[2].context.span_id == metadata[3].context.span_id
    assert metadata[0].context.span_id != metadata[2].context.span_id
    assert len({item.context.trace_id for item in metadata}) == 1
    assert metadata[2].causation_event_id == events[1].event_id
