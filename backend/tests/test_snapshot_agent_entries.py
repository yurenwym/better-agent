"""Shipping expert workers, with provider sends checked after commit."""
import asyncio
import json

import pytest

from app.agents import AgentTaskService, LiveExpertModel, ManagedAgentWorker
from app.model_control import ModelControlStore, RoutedModelGateway
from app.model_input_snapshot_store import ModelInputSnapshotStore, SnapshotMissing
from app.evolution import LiveSafetyJudge
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_snapshot_gateway import _answer, _configured_control_plane


@pytest.mark.asyncio
async def test_historical_expert_without_root_does_not_send_or_create_identity(tmp_path, monkeypatch):
    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    service = AgentTaskService(db)
    append = service._event

    def historical(connection, run_id, task_id, kind, actor, data):
        if kind == "agent.run.created":
            data = {key: value for key, value in data.items() if key != "execution_context"}
        return append(connection, run_id, task_id, kind, actor, data)

    monkeypatch.setattr(service, "_event", historical)
    run = service.create_run("local-user", "topic", {}, bundle.id, idempotency_key="legacy",
        expert_roles=("critic",), append_thread_message=False)
    sends = []

    async def execute(*args, **kwargs):
        sends.append(True)
        return _answer("unused")

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    worker = ManagedAgentWorker(service, LiveExpertModel(gateway))
    for _ in range(3):
        await worker.run_once()
    assert sends == []
    assert service.get_run(run["id"])["status"] == "FAILED"
    created = service.events(run["id"])[0]
    assert "execution_context" not in created["data"]


@pytest.mark.asyncio
async def test_conversation_expert_models_inherit_source_trace(tmp_path, monkeypatch):
    from app.behavior import BehaviorBundleService
    from app.conversation import ConversationService
    from app.harness_context_store import HarnessContextStore
    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    BehaviorBundleService(db).activate("stable", bundle.id, "expert-trace")
    conversation = ConversationService(db)
    thread = conversation.create_thread("expert")
    source = conversation.accept_turn(thread.id, "source", "expert request", [])
    service = AgentTaskService(db, thread_events=conversation.events)
    run = service.create_run("local-user", "topic", {}, bundle.id, thread_id=thread.id,
        idempotency_key="trace", parent_turn_id=source.turn_id, append_thread_message=False,
        expert_roles=("critic",))
    seen = []

    async def execute(profile, request, **kwargs):
        seen.append(gateway.current_call_context())
        return _answer(json.dumps({"summary": "result", "findings": [], "risks": [], "open_questions": []})
            if request.purpose.startswith("expert_") else "summary")

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    worker = ManagedAgentWorker(service, LiveExpertModel(gateway))
    for _ in range(3):
        assert await worker.run_once()
    assert service.get_run(run["id"])["status"] == "SUCCEEDED"
    root = HarnessContextStore(db).load_turn_context(source.turn_id)
    assert len(seen) == 2
    assert all(context.harness and context.harness.trace_id == root.trace_id for context in seen)
    assert all(context.turn_id == source.turn_id and context.owner_id == root.owner_id for context in seen)
    assert len({context.task_id for context in seen}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("user_task", [False, True])
async def test_t09_t10_expert_workers_isolate_interleaved_owners_and_judges(tmp_path, monkeypatch, user_task):
    await exercise_interleaved_experts(tmp_path, monkeypatch, user_task)


async def exercise_interleaved_experts(tmp_path, monkeypatch, user_task, database=None):
    from datetime import datetime, timedelta, timezone
    from app.costs import CostService

    db, bundle_a, _ = _configured_control_plane(tmp_path, monkeypatch, owner_id="owner-a", database=database)
    _, bundle_b, _ = _configured_control_plane(tmp_path, monkeypatch, database=db, owner_id="owner-b")
    pinned = {"owner-a": bundle_a, "owner-b": bundle_b}
    costs = CostService(db)
    roots = {owner: costs.create_root_budget(owner, "turn", f"expert-{owner}", max_attempts=20,
        deadline_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), limit_microusd=100000)["id"]
        for owner in pinned} if db.backend == "postgresql" else {}

    service = AgentTaskService(db)
    runs = {owner: service.create_run(
        owner, f"Only {owner}", {"private": owner, "task_mode": "user_task" if user_task else "advice"},
        bundle.id, idempotency_key=owner, expert_roles=("planner", "critic"), append_thread_message=False,
        root_budget_id=roots.get(owner),
    ) for owner, bundle in pinned.items()}
    observations = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(profile, request, **kwargs):
        context = gateway.current_call_context()
        owner = context.owner_id
        if request.purpose.startswith("expert_"):
            payload = json.loads(request.messages[1]["content"])
            assert payload["context"]["private"] == owner
            response = _answer(json.dumps({"summary": f"Evidence {owner}", "findings": [], "risks": [], "open_questions": []}))
        elif request.purpose == "judge_expert_output":
            assert f"Evidence {owner}" in request.messages[-1]["content"]
            response = _answer("safe")
        else:
            assert f"Evidence {owner}" in json.dumps(request.messages)
            assert f"Evidence {'owner-b' if owner == 'owner-a' else 'owner-a'}" not in json.dumps(request.messages)
            response = _answer("Combined")
        recorder = CommittedSnapshotTransport(db, owner, "CS-AG-02" if request.role == "judge_safety" else "CS-AG-01", response)
        result = await recorder(profile, request, **kwargs)
        observations.extend(recorder.observations)
        if owner == "owner-a" and request.purpose == "expert_critic":
            entered.set()
            await release.wait()
            raise RuntimeError("one expert failed")
        return result

    gateway = RoutedModelGateway(db, ModelControlStore(db, costs=costs), execute_attempt=execute)
    workers = [ManagedAgentWorker(service, LiveExpertModel(gateway), safety_judge=LiveSafetyJudge(gateway)) for _ in range(2)]
    assert await workers[0].run_once()  # A fan-out
    assert await workers[1].run_once()  # B fan-out
    # Claim explicitly by persisted task ID so ordering never depends on UUID ties.
    with db.connection() as connection:
        target = connection.execute("SELECT id FROM agent_tasks WHERE agent_run_id=? AND role='critic'", (runs["owner-a"]["id"],)).fetchone()[0]
    # Queue order is time based; move only the selected queued task to the front.
    with db.transaction() as connection:
        connection.execute("UPDATE agent_tasks SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (target,))
    pending = asyncio.create_task(workers[0].run_once())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(12):
            if not await workers[1].run_once():
                break
            assert gateway.current_call_context() is None
    finally:
        release.set()
        await pending
    for _ in range(12):
        if not await workers[1].run_once():
            break
    assert gateway.current_call_context() is None
    assert len(observations) == 9  # 4 experts (one fails), 3 judges, 2 syntheses
    assert len({item["snapshot_id"] for item in observations}) == 9
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT i.*,r.owner_id AS run_owner,r.runtime_bundle_id AS run_bundle "
            "FROM model_invocations i JOIN agent_tasks t ON t.id=i.agent_task_id "
            "JOIN agent_runs r ON r.id=t.agent_run_id"
        ).fetchall()
    assert len(rows) == 9
    assert all(row["owner_id"] == row["run_owner"] and row["runtime_bundle_id"] == row["run_bundle"] for row in rows)
    from app.execution_context import deserialize_context
    with db.connection() as connection:
        trace_by_owner = {}
        for owner, run in runs.items():
            created = connection.execute(
                "SELECT data_json FROM agent_events WHERE agent_run_id=? AND type='agent.run.created'", (run["id"],),
            ).fetchone()
            trace_by_owner[owner] = deserialize_context(json.loads(created["data_json"])["execution_context"]).trace_id
    assert len(set(trace_by_owner.values())) == 2
    assert all(deserialize_context(row["execution_context_json"]).trace_id == trace_by_owner[row["owner_id"]] for row in rows)
    if roots:
        assert all(row["root_budget_id"] == roots[row["owner_id"]] for row in rows)
        with db.connection() as connection:
            budgets = connection.execute("SELECT owner_id,id,attempts_started FROM task_budget_roots").fetchall()
        assert {row["owner_id"]: row["attempts_started"] for row in budgets} == {"owner-a": 4, "owner-b": 5}
    assert sum(row["purpose"] == "judge_expert_output" and row["role"] == "judge_safety" for row in rows) == 3
    for item in observations:
        other = "owner-b" if item["owner_id"] == "owner-a" else "owner-a"
        with pytest.raises(SnapshotMissing):
            ModelInputSnapshotStore(db).load(other, item["snapshot_id"])
    assert service.get_run(runs["owner-b"]["id"])["status"] == "SUCCEEDED"
