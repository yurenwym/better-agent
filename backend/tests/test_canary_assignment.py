from datetime import datetime, timedelta, timezone

from app.agents import AgentTaskService
from app.behavior import BehaviorBundleService
from app.db import Database
from app.evolution import EvolutionService


def _approved_canary(tmp_path):
    db = Database(tmp_path / "canary-assignment.db")
    bundles = BehaviorBundleService(db)
    base = bundles.ensure({"prompt": "v1", "core_policy": "frozen", "permissions": ["read"]})
    target = bundles.ensure({"prompt": "v2", "core_policy": "frozen", "permissions": ["read"]})
    bundles.activate("stable", base.id, "stable-v1")
    evolution = EvolutionService(db, bundles)
    evidence = [
        evolution.record_experience(
            task_type="expert", outcome="needs improvement", lineage_group_hash=f"lineage-{index}",
            source_content_hash=f"source-{index}", runtime_bundle_id=base.id,
            dataset_partition="DISCOVERY", idempotency_key=f"experience-{index}",
        )
        for index in range(3)
    ]
    candidate = evolution.propose_candidate(
        candidate_type="prompt", experience_ids=[item["id"] for item in evidence],
        base_bundle_id=base.id, target_bundle_id=target.id,
        proposed_content={"prompt": "v2"}, permission_diff={"added": [], "removed": []},
        reason="independent evidence", idempotency_key="candidate",
    )
    evaluation = evolution.evaluate(
        candidate["id"], expected_version=candidate["version"],
        deterministic_checks={"schema": True, "safety": True}, metrics={"success_rate": 1.0},
        eval_set_digest="eval-set", evaluator_digest="deterministic-v1", idempotency_key="evaluation",
    )
    evaluated = evolution.get_candidate(candidate["id"])
    approval = evolution.approve(
        candidate["id"], expected_version=evaluated["version"], evaluation_id=evaluation["id"],
        candidate_digest=evaluated["proposed_digest"], evaluation_report_digest=evaluation["report_digest"],
        permission_diff_digest=evaluated["permission_diff_digest"], target_bundle_digest=evaluated["target_bundle_digest"],
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        actor="user", idempotency_key="approval",
    )
    approved = evolution.get_candidate(candidate["id"])
    deployment = evolution.start_canary(
        candidate["id"], expected_version=approved["version"], approval_id=approval["id"],
        allocation_percent=100, assignment_unit="run", idempotency_key="canary",
    )
    return db, evolution, base, target, deployment


def test_new_run_uses_active_canary_bundle_without_inventing_a_safety_verdict(tmp_path):
    db, evolution, base, target, deployment = _approved_canary(tmp_path)
    tasks = AgentTaskService(db, evolution=evolution)

    run = tasks.create_run("local-user", "goal", {"goal": "x"}, base.id, idempotency_key="run")
    assert run["runtime_bundle_id"] == target.id
    repeated = tasks.create_run("local-user", "goal", {"goal": "x"}, base.id, idempotency_key="run")
    assert repeated["id"] == run["id"]

    with db.connection() as connection:
        exposures = connection.execute(
            "SELECT * FROM canary_exposures WHERE deployment_id=?", (deployment["id"],)
        ).fetchall()
    assert len(exposures) == 1
    assert exposures[0]["run_id"] == run["id"]
    assert exposures[0]["cohort"] == "challenger"
    assert exposures[0]["success"] is None

    task = tasks.claim_next("worker", 30)
    tasks.complete(task["id"], "worker", task["lease_epoch"], "answer", {"text": "done"})
    with db.connection() as connection:
        exposure = connection.execute(
            "SELECT success,safety_pass FROM canary_exposures WHERE deployment_id=? AND run_id=?",
            (deployment["id"], run["id"]),
        ).fetchone()
    assert exposure["success"] == 1
    assert exposure["safety_pass"] is None


def test_canary_completion_without_safety_evidence_stays_pending_and_cannot_promote(tmp_path):
    db, evolution, base, _, deployment = _approved_canary(tmp_path)
    tasks = AgentTaskService(db, evolution=evolution)
    run = tasks.create_run("local-user", "unknown safety", {}, base.id, idempotency_key="unknown-safety")

    evolution.finish_run_exposure(run["id"], success=True)

    with db.connection() as connection:
        exposure = connection.execute(
            "SELECT success,safety_pass FROM canary_exposures WHERE deployment_id=? AND run_id=?",
            (deployment["id"], run["id"]),
        ).fetchone()
        state = connection.execute("SELECT status FROM canary_deployments WHERE id=?", (deployment["id"],)).fetchone()
    assert exposure["success"] == 1
    assert exposure["safety_pass"] is None
    assert state["status"] == "ACTIVE"
    assert evolution.get_candidate(deployment["candidate_id"])["canary"]["promotable"] is False


def test_run_without_active_canary_uses_stable_and_cancel_records_failure(tmp_path):
    db, evolution, base, _, deployment = _approved_canary(tmp_path)
    with db.transaction() as connection:
        connection.execute("UPDATE canary_deployments SET status='STOPPED' WHERE id=?", (deployment["id"],))

    tasks = AgentTaskService(db, evolution=evolution)
    stable = tasks.create_run("local-user", "stable", {}, base.id, idempotency_key="stable-run")
    assert stable["runtime_bundle_id"] == base.id

    with db.transaction() as connection:
        connection.execute("UPDATE canary_deployments SET status='ACTIVE' WHERE id=?", (deployment["id"],))
    canary = tasks.create_run("local-user", "cancel", {}, base.id, idempotency_key="cancel-run")
    tasks.cancel_run(canary["id"], "user cancelled")
    with db.connection() as connection:
        exposure = connection.execute(
            "SELECT success FROM canary_exposures WHERE deployment_id=? AND run_id=?",
            (deployment["id"], canary["id"]),
        ).fetchone()
    assert exposure["success"] == 0


def test_challenger_safety_failure_atomically_rolls_back_canary(tmp_path):
    db, evolution, base, _, deployment = _approved_canary(tmp_path)
    tasks = AgentTaskService(db, evolution=evolution)
    run = tasks.create_run("local-user", "unsafe", {}, base.id, idempotency_key="unsafe-run")

    evolution.finish_run_exposure(run["id"], success=False, safety_pass=False)

    with db.connection() as connection:
        state = connection.execute(
            "SELECT d.status,c.status candidate_status FROM canary_deployments d "
            "JOIN evolution_candidates c ON c.id=d.candidate_id WHERE d.id=?", (deployment["id"],)
        ).fetchone()
        event = connection.execute(
            "SELECT type FROM evolution_events WHERE type='evolution.canary.auto_rolled_back'"
        ).fetchone()
    assert state["status"] == "ROLLED_BACK"
    assert state["candidate_status"] == "ROLLED_BACK"
    assert evolution.bundles.active("canary").id == base.id
    assert event["type"] == "evolution.canary.auto_rolled_back"
