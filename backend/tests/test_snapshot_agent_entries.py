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
