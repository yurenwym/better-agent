from datetime import datetime, timedelta, timezone

import pytest

from app.agents import AgentTaskConflict, AgentTaskService
from app.behavior import BehaviorBundleService
from app.db import Database


def service(tmp_path):
    db = Database(tmp_path / "agent.db")
    bundle = BehaviorBundleService(db).ensure({"code": "test"})
    return db, AgentTaskService(db), bundle.id


def test_claim_takeover_fences_the_expired_worker_and_commits_one_artifact(tmp_path):
    db, tasks, bundle_id = service(tmp_path)
    run = tasks.create_run("local-user", "goal", {"goal": "x"}, bundle_id, idempotency_key="run-1")
    first = tasks.claim_next("worker-a", 30)
    assert first and first["id"] == run["coordinator_task_id"] and first["lease_epoch"] == 1
    assert tasks.get_run(run["id"])["status"] == "RUNNING"
    checkpoint = tasks.save_checkpoint(first["id"], "worker-a", 1, {"phase":"start"})
    assert tasks.latest_checkpoint(first["id"])["id"] == checkpoint["id"]
    with db.transaction() as connection:
        connection.execute("UPDATE agent_tasks SET lease_until=? WHERE id=?", ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), first["id"]))
    second = tasks.claim_next("worker-b", 30)
    assert second and second["lease_epoch"] == 2
    with pytest.raises(PermissionError, match="lease lost"):
        tasks.complete(first["id"], "worker-a", 1, "answer", {"text": "late"})
    with pytest.raises(PermissionError, match="lease lost"):
        tasks.save_checkpoint(first["id"], "worker-a", 1, {"phase":"late"})
    completed = tasks.complete(second["id"], "worker-b", 2, "answer", {"text": "ok"})
    assert completed["status"] == "SUCCEEDED"
    assert tasks.artifact(completed["result_artifact_id"])["content"] == {"text": "ok"}
    assert [event["seq"] for event in tasks.events(run["id"])] == list(range(1, len(tasks.events(run["id"])) + 1))


def test_fan_out_is_idempotent_and_all_done_wakes_parent_once(tmp_path):
    _, tasks, bundle_id = service(tmp_path)
    run = tasks.create_run("local-user", "goal", {"goal": "x"}, bundle_id, idempotency_key="run-2")
    parent = tasks.claim_next("coordinator", 30)
    children = [
        {"child_key": "research", "role": "researcher", "objective": "research", "output_schema": "brief.v1"},
        {"child_key": "critic", "role": "critic", "objective": "critic", "output_schema": "critique.v1"},
    ]
    created = tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], children, "ALL_DONE")
    repeated = tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], children, "ALL_DONE")
    assert [item["id"] for item in created] == [item["id"] for item in repeated]
    one = tasks.claim_next("one", 30); tasks.complete(one["id"], "one", one["lease_epoch"], "brief", {"ok": True})
    two = tasks.claim_next("two", 30); tasks.fail(two["id"], "two", two["lease_epoch"], "MODEL_ERROR", retryable=False)
    resumed = tasks.claim_next("coordinator-2", 30)
    assert resumed and resumed["id"] == parent["id"]
    statuses = {item["id"]: item["status"] for item in tasks.children(parent["id"])}
    assert statuses[one["id"]] == "SUCCEEDED" and statuses[two["id"]] == "FAILED"


def test_cancel_fences_descendants_and_parallel_budget_cannot_overspend(tmp_path):
    _, tasks, bundle_id = service(tmp_path)
    run = tasks.create_run("local-user", "goal", {"goal": "x"}, bundle_id, budget_units=2, idempotency_key="run-3")
    parent = tasks.claim_next("coordinator", 30)
    with pytest.raises(AgentTaskConflict, match="budget"):
        tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], [
            {"child_key": "a", "role": "r", "objective": "a", "budget_units": 2},
            {"child_key": "b", "role": "r", "objective": "b", "budget_units": 2},
        ], "ALL_SUCCESS")
    children = tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], [
        {"child_key": "a", "role": "r", "objective": "a", "budget_units": 1},
    ], "ALL_SUCCESS")
    child = tasks.claim_next("child", 30)
    tasks.cancel_run(run["id"], "user cancelled")
    with pytest.raises(PermissionError, match="lease lost"):
        tasks.complete(child["id"], "child", child["lease_epoch"], "x", {"late": True})
    assert tasks.get_task(children[0]["id"])["status"] == "CANCELLED"
    assert any(event["type"] == "agent.task.late_result_rejected" for event in tasks.events(run["id"]))


def test_expert_artifact_must_match_the_task_output_schema(tmp_path):
    _, tasks, bundle_id = service(tmp_path)
    run = tasks.create_run("local-user", "goal", {"goal": "x"}, bundle_id, idempotency_key="schema-run")
    parent = tasks.claim_next("coordinator", 30)
    tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], [
        {"child_key": "research", "role": "researcher", "objective": "research", "output_schema": "expert_result.v1"},
    ], "ALL_DONE")
    child = tasks.claim_next("worker", 30)

    with pytest.raises(ValueError, match="expert_result.v1"):
        tasks.complete(child["id"], "worker", child["lease_epoch"], "researcher_result", {"raw": "not structured"})

    assert tasks.get_task(child["id"])["status"] == "RUNNING"
    valid = {
        "summary": "result", "findings": [{"text": "fact", "confidence": .8, "source_refs": []}],
        "risks": [], "open_questions": [],
    }
    assert tasks.complete(child["id"], "worker", child["lease_epoch"], "researcher_result", valid)["status"] == "SUCCEEDED"


def test_cancel_cannot_overwrite_a_terminal_run(tmp_path):
    _, tasks, bundle_id = service(tmp_path)
    run = tasks.create_run("local-user", "goal", {}, bundle_id, idempotency_key="terminal-cancel")
    task = tasks.claim_next("worker", 30)
    tasks.complete(task["id"], "worker", task["lease_epoch"], "answer", {"text": "done"})

    with pytest.raises(AgentTaskConflict, match="already finished"):
        tasks.cancel_run(run["id"], "late cancel")

    assert tasks.get_run(run["id"])["status"] == "SUCCEEDED"


def test_run_idempotency_and_fanout_bind_the_full_request(tmp_path):
    db, tasks, bundle_id = service(tmp_path)
    run = tasks.create_run("local-user", "goal", {"x": 1}, bundle_id, idempotency_key="bound-run")
    with pytest.raises(AgentTaskConflict, match="payload changed"):
        tasks.create_run("local-user", "different", {"x": 1}, bundle_id, idempotency_key="bound-run")
    other_bundle = BehaviorBundleService(db).ensure({"code":"other"})
    with pytest.raises(AgentTaskConflict, match="payload changed"):
        tasks.create_run("local-user", "goal", {"x": 1}, other_bundle.id, idempotency_key="bound-run")
    parent = tasks.claim_next("coordinator", 30)
    children = [{"child_key":"one", "role":"researcher", "objective":"first", "budget_units":1}]
    tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], children, "ALL_DONE")
    with pytest.raises(AgentTaskConflict, match="payload changed"):
        tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], [
            {"child_key":"one", "role":"critic", "objective":"changed", "budget_units":1},
        ], "ALL_DONE")


def test_last_expired_attempt_finishes_the_run(tmp_path):
    db, tasks, bundle_id = service(tmp_path)
    run = tasks.create_run("local-user", "goal", {}, bundle_id, idempotency_key="expired-run")
    task = tasks.claim_next("worker-a", 30)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE agent_tasks SET max_attempts=1,lease_until=? WHERE id=?",
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), task["id"]),
        )
    assert tasks.claim_next("worker-b", 30) is None
    assert tasks.get_run(run["id"])["status"] == "FAILED"


def test_expert_artifact_rejects_unapproved_fields(tmp_path):
    _, tasks, bundle_id = service(tmp_path)
    run = tasks.create_run("local-user", "goal", {}, bundle_id, idempotency_key="redact-schema")
    parent = tasks.claim_next("coordinator", 30)
    tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], [
        {"child_key": "critic", "role": "critic", "objective": "check", "output_schema": "expert_result.v1"},
    ], "ALL_DONE")
    child = tasks.claim_next("worker", 30)
    with pytest.raises(ValueError, match="unsupported"):
        tasks.complete(child["id"], "worker", child["lease_epoch"], "critic_result", {
            "summary": "ok", "findings": [], "risks": [], "open_questions": [], "chain_of_thought": "hidden",
        })
