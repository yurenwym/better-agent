import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.agents import AgentTaskService
from app.behavior import BehaviorBundleService
from app.db import Database
from app.evolution import EvolutionService


def _approved_canary(tmp_path):
    db = Database(tmp_path / "canary-assignment.db")
    bundles = BehaviorBundleService(db)
    frozen = {
        "core_policy": "frozen", "permissions": ["read"],
        "model_routing": {"policy_id": "routing-v1", "digest": "bundle-routing-digest"},
        "skills": {"coach": "skill-package-digest"},
    }
    base = bundles.ensure({"prompt": "v1", **frozen})
    target = bundles.ensure({"prompt": "v2", **frozen})
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


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _record_model_ledger(db, run_id: str, bundle_id: str) -> None:
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) "
            "VALUES ('profile','local-user','canary','ACTIVE','now','now')"
        )
        for version_id, version, config_digest in (("profile-v1", 1, "profile-config-a"), ("profile-v2", 2, "profile-config-b")):
            connection.execute(
                "INSERT INTO model_profile_versions(id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,"
                "capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at) "
                "VALUES (?,?,?,'openai_compatible','test','https://example.test','model','TEST_KEY','[\"text\"]',8192,1024,30,2,?,'now')",
                (version_id, "profile", version, config_digest),
            )
        for invocation_id in ("invocation-1", "invocation-2"):
            connection.execute(
                "INSERT INTO model_invocations(id,owner_id,run_id,role,purpose,runtime_bundle_id,routing_policy_digest,route_snapshot_json,"
                "request_digest,tool_schema_digest,context_snapshot_digest,status,idempotency_key,created_at,finished_at) "
                "VALUES (?, 'local-user', ?, 'planner', 'canary', ?, 'route-digest', '{}', ?, 'tools', 'context', 'SUCCEEDED', ?, "
                "'2026-08-28T00:00:00+00:00','2026-08-28T00:00:05+00:00')",
                (invocation_id, run_id, bundle_id, f"request-{invocation_id}", f"key-{invocation_id}"),
            )
        attempts = (
            ("attempt-1", "invocation-1", 1, "profile-v1", "2026-08-28T00:00:00+00:00", "2026-08-28T00:00:01+00:00", 120),
            ("attempt-2", "invocation-1", 2, "profile-v2", "2026-08-28T00:00:01+00:00", "2026-08-28T00:00:04+00:00", 230),
            ("attempt-3", "invocation-2", 1, "profile-v2", "2026-08-28T00:00:02+00:00", None, 50),
        )
        for attempt_id, invocation_id, ordinal, profile_version_id, started_at, first_token_at, cost in attempts:
            connection.execute(
                "INSERT INTO model_attempts(id,invocation_id,ordinal,reason,profile_version_id,provider_protocol,request_digest,status,"
                "started_at,first_token_at,finished_at,usage_status,cost_status,cost_microusd) "
                "VALUES (?,?,?,'primary',?,'openai_compatible',?,'SUCCEEDED',?,?,'2026-08-28T00:00:05+00:00','COMPLETE','ACTUAL',?)",
                (attempt_id, invocation_id, ordinal, profile_version_id, f"request-{attempt_id}", started_at, first_token_at, 999_999),
            )
            connection.execute(
                "INSERT INTO cost_ledger(id,owner_id,period_kind,period_key,invocation_id,attempt_id,entry_type,amount_microusd,"
                "cost_status,reason,idempotency_key,created_at) VALUES (?, 'local-user', 'INVOCATION', ?, ?, ?, 'CHARGE', ?, "
                "'ACTUAL', 'settled', ?, '2026-08-28T00:00:05+00:00')",
                (f"cost-{attempt_id}", invocation_id, invocation_id, attempt_id, cost, f"charge-{attempt_id}"),
            )


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
    _record_model_ledger(db, run["id"], run["runtime_bundle_id"])

    evolution.finish_run_exposure(run["id"], success=False, safety_pass=False)

    with db.connection() as connection:
        state = connection.execute(
            "SELECT d.status,c.status candidate_status FROM canary_deployments d "
            "JOIN evolution_candidates c ON c.id=d.candidate_id WHERE d.id=?", (deployment["id"],)
        ).fetchone()
        event = connection.execute(
            "SELECT type FROM evolution_events WHERE type='evolution.canary.auto_rolled_back'"
        ).fetchone()
        exposure = connection.execute(
            "SELECT * FROM canary_exposures WHERE deployment_id=? AND run_id=?",
            (deployment["id"], run["id"]),
        ).fetchone()
    assert state["status"] == "ROLLED_BACK"
    assert state["candidate_status"] == "ROLLED_BACK"
    assert evolution.bundles.active("canary").id == base.id
    assert evolution.bundles.active("stable").id == base.id
    assert event["type"] == "evolution.canary.auto_rolled_back"
    assert exposure["quality_outcome"] == "failure"
    assert exposure["safety_outcome"] == "failed"
    assert exposure["ttft_ms"] == 2000
    assert exposure["ttft_p95_ms"] == 3000
    assert exposure["invocation_count"] == 2
    assert exposure["attempt_count"] == 3
    assert exposure["cost_microusd"] == 400
    assert exposure["routing_policy_digest"] == _digest(["route-digest"])
    assert exposure["profile_digest"] == _digest(["profile-config-a", "profile-config-b"])
    assert exposure["skill_digest"] == _digest({"coach": "skill-package-digest"})
    rollback = evolution.get_candidate(deployment["candidate_id"])["rollback"]
    assert rollback["kind"] == "safety_auto"
    assert rollback["actor"] == "safety-gate"
    assert rollback["reason"] == "Canary safety check failed"


def test_canary_safety_rollback_is_atomic_with_authoritative_metrics(tmp_path, monkeypatch):
    db, evolution, base, _, deployment = _approved_canary(tmp_path)
    tasks = AgentTaskService(db, evolution=evolution)
    run = tasks.create_run("local-user", "unsafe", {}, base.id, idempotency_key="atomic-run")
    _record_model_ledger(db, run["id"], run["runtime_bundle_id"])

    def fail_event(*args, **kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(evolution, "_event", fail_event)
    with pytest.raises(RuntimeError, match="event write failed"):
        evolution.finish_run_exposure(run["id"], success=False, safety_pass=False)

    with db.connection() as connection:
        exposure = connection.execute(
            "SELECT success,safety_pass,quality_outcome,cost_microusd FROM canary_exposures WHERE deployment_id=? AND run_id=?",
            (deployment["id"], run["id"]),
        ).fetchone()
        deployment_status = connection.execute(
            "SELECT status FROM canary_deployments WHERE id=?", (deployment["id"],)
        ).fetchone()["status"]
        candidate_status = connection.execute(
            "SELECT status FROM evolution_candidates WHERE id=?", (deployment["candidate_id"],)
        ).fetchone()["status"]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM evolution_events WHERE type='evolution.canary.auto_rolled_back'"
        ).fetchone()[0]

    assert dict(exposure) == {"success": None, "safety_pass": None, "quality_outcome": None, "cost_microusd": None}
    assert deployment_status == "ACTIVE"
    assert candidate_status == "CANARY"
    assert evolution.bundles.active("canary").id == deployment["challenger_bundle_id"]
    assert event_count == 0


def test_finished_canary_exposure_is_frozen_on_replay(tmp_path):
    db, evolution, base, _, deployment = _approved_canary(tmp_path)
    tasks = AgentTaskService(db, evolution=evolution)
    run = tasks.create_run("local-user", "done", {}, base.id, idempotency_key="replayed-run")

    evolution.finish_run_exposure(run["id"], success=True, safety_pass=True)
    evolution.finish_run_exposure(run["id"], success=False, safety_pass=False)

    with db.connection() as connection:
        exposure = connection.execute(
            "SELECT success,safety_pass,quality_outcome,safety_outcome FROM canary_exposures "
            "WHERE deployment_id=? AND run_id=?", (deployment["id"], run["id"]),
        ).fetchone()
        status = connection.execute(
            "SELECT status FROM canary_deployments WHERE id=?", (deployment["id"],)
        ).fetchone()["status"]
    assert dict(exposure) == {
        "success": 1, "safety_pass": 1, "quality_outcome": "success", "safety_outcome": "passed",
    }
    assert status == "ACTIVE"
