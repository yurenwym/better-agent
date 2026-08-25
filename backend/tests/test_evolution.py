from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.behavior import BehaviorBundleService
from app.db import Database
from app.evolution import EvolutionConflict, EvolutionGateError, EvolutionService


def setup_service(tmp_path):
    db = Database(tmp_path / "evolution.db")
    bundles = BehaviorBundleService(db)
    base = bundles.ensure({"prompt": "v1", "core_policy": "frozen", "permissions": ["read"]})
    target = bundles.ensure({"prompt": "v2", "core_policy": "frozen", "permissions": ["read"]})
    bundles.activate("stable", base.id, "stable-v1")
    return db, bundles, EvolutionService(db, bundles, minimum_canary_samples=3), base, target


def experiences(service, bundle_id, count=3):
    return [
        service.record_experience(
            task_type="conversation",
            outcome="asked unnecessarily",
            lineage_group_hash=f"lineage-{index}",
            source_content_hash=f"source-{index}",
            runtime_bundle_id=bundle_id,
            dataset_partition="DISCOVERY",
            idempotency_key=f"experience-{index}",
        )
        for index in range(count)
    ]


def candidate(service, base, target, evidence):
    return service.propose_candidate(
        candidate_type="prompt",
        experience_ids=[item["id"] for item in evidence],
        base_bundle_id=base.id,
        target_bundle_id=target.id,
        proposed_content={"prompt": "v2"},
        permission_diff={"added": [], "removed": []},
        reason="three independent failures",
        idempotency_key="candidate-1",
    )


def test_experience_lineage_requires_three_independent_discovery_records_and_candidate_is_frozen(tmp_path):
    db, _, service, base, target = setup_service(tmp_path)
    evidence = experiences(service, base.id, 2)
    with pytest.raises(EvolutionGateError, match="three independent"):
        candidate(service, base, target, evidence)

    third = experiences(service, base.id, 3)[-1]
    item = candidate(service, base, target, [*evidence, third])
    assert item["status"] == "READY_FOR_EVAL"
    assert len(item["experience_ids"]) == 3

    with pytest.raises(EvolutionConflict, match="lineage partition"):
        service.record_experience(
            task_type="conversation",
            outcome="copy",
            lineage_group_hash="lineage-0",
            source_content_hash="source-copy",
            runtime_bundle_id=base.id,
            dataset_partition="HOLDOUT",
            idempotency_key="partition-copy",
        )

    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        with db.transaction() as connection:
            connection.execute("UPDATE evolution_candidates SET proposed_digest='changed' WHERE id=?", (item["id"],))


def test_deterministic_gate_approval_binding_canary_samples_and_atomic_rollback(tmp_path):
    _, bundles, service, base, target = setup_service(tmp_path)
    item = candidate(service, base, target, experiences(service, base.id))
    evaluation = service.evaluate(
        item["id"],
        expected_version=item["version"],
        deterministic_checks={"schema": True, "safety": True, "permissions": True},
        metrics={"success_rate": 1.0},
        eval_set_digest="eval-set-v1",
        evaluator_digest="deterministic-v1",
        idempotency_key="evaluate-1",
    )
    evaluated = service.get_candidate(item["id"])
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    with pytest.raises(EvolutionConflict, match="binding"):
        service.approve(
            item["id"], expected_version=evaluated["version"], evaluation_id=evaluation["id"],
            candidate_digest=evaluated["proposed_digest"], evaluation_report_digest="wrong",
            permission_diff_digest=evaluated["permission_diff_digest"], target_bundle_digest=evaluated["target_bundle_digest"],
            expires_at=expires_at, actor="user", idempotency_key="bad-approval",
        )

    decision = service.approve(
        item["id"], expected_version=evaluated["version"], evaluation_id=evaluation["id"],
        candidate_digest=evaluated["proposed_digest"], evaluation_report_digest=evaluation["report_digest"],
        permission_diff_digest=evaluated["permission_diff_digest"], target_bundle_digest=evaluated["target_bundle_digest"],
        expires_at=expires_at, actor="user", idempotency_key="approval-1",
    )
    approved = service.get_candidate(item["id"])
    deployment = service.start_canary(
        item["id"], expected_version=approved["version"], approval_id=decision["id"],
        allocation_percent=100, assignment_unit="run", idempotency_key="canary-1",
    )
    assert bundles.active("canary").id == target.id

    for index in range(2):
        service.record_exposure(deployment["id"], f"run-{index}", f"subject-{index}", success=True, safety_pass=True)
    canary = service.get_candidate(item["id"])
    with pytest.raises(EvolutionGateError, match="samples"):
        service.promote(item["id"], expected_version=canary["version"], idempotency_key="promote-too-early")

    service.record_exposure(deployment["id"], "run-2", "subject-2", success=True, safety_pass=True)
    promoted = service.promote(item["id"], expected_version=canary["version"], idempotency_key="promote-1")
    assert promoted["status"] == "PROMOTED"
    assert bundles.active("stable").id == target.id

    rolled_back = service.rollback(
        item["id"], expected_version=promoted["version"], reason="operator rollback", idempotency_key="rollback-1"
    )
    assert rolled_back["status"] == "ROLLED_BACK"
    assert bundles.active("stable").id == base.id


def test_failed_deterministic_evaluation_and_expired_approval_cannot_deploy(tmp_path):
    _, _, service, base, target = setup_service(tmp_path)
    first = candidate(service, base, target, experiences(service, base.id))
    failed = service.evaluate(
        first["id"], expected_version=first["version"], deterministic_checks={"safety": False}, metrics={},
        eval_set_digest="set", evaluator_digest="deterministic-v1", idempotency_key="failed-eval",
    )
    assert failed["deterministic_pass"] is False
    with pytest.raises(EvolutionGateError, match="deterministic"):
        service.approve_current(first["id"], expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), idempotency_key="no")

    second = service.propose_candidate(
        candidate_type="prompt", experience_ids=[item["id"] for item in experiences(service, base.id, 6)[3:]],
        base_bundle_id=base.id, target_bundle_id=target.id, proposed_content={"prompt":"v2"},
        permission_diff={"added":[]}, reason="next independent set", idempotency_key="candidate-2",
    )
    service.evaluate(
        second["id"], expected_version=second["version"], deterministic_checks={"safety":True}, metrics={},
        eval_set_digest="set-2", evaluator_digest="deterministic-v1", idempotency_key="eval-2",
    )
    with pytest.raises(EvolutionGateError, match="future"):
        service.approve_current(
            second["id"], expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), idempotency_key="expired"
        )


def test_evolution_events_are_append_only(tmp_path):
    db, _, service, base, _ = setup_service(tmp_path)
    service.record_experience(
        task_type="conversation", outcome="failure", lineage_group_hash="immutable-lineage",
        source_content_hash="immutable-source", runtime_bundle_id=base.id,
        dataset_partition="DISCOVERY", idempotency_key="immutable-event",
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db.transaction() as connection:
            connection.execute("UPDATE evolution_events SET type='changed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with db.transaction() as connection:
            connection.execute("DELETE FROM evolution_events")


def test_candidate_content_must_exactly_describe_target_bundle_diff(tmp_path):
    _, _, service, base, target = setup_service(tmp_path)
    with pytest.raises(EvolutionGateError, match="target bundle diff"):
        service.propose_candidate(
            candidate_type="prompt", experience_ids=[item["id"] for item in experiences(service, base.id)],
            base_bundle_id=base.id, target_bundle_id=target.id,
            proposed_content={"prompt":"harmless display text"}, permission_diff={"added":[]},
            reason="mismatch", idempotency_key="mismatch",
        )

    deleted = service.bundles.ensure({"core_policy":"frozen", "permissions":["read"]})
    deletion = service.propose_candidate(
        candidate_type="prompt", experience_ids=[item["id"] for item in experiences(service, base.id, 6)[3:]],
        base_bundle_id=base.id, target_bundle_id=deleted.id,
        proposed_content={"prompt":None}, permission_diff={"added":[]}, reason="delete",
        idempotency_key="deletion",
    )
    assert deletion["proposed_content"] == {"prompt": None}


def test_only_one_canary_is_active_and_old_promotion_cannot_rollback_new_stable(tmp_path):
    _, bundles, service, base, target = setup_service(tmp_path)
    item = candidate(service, base, target, experiences(service, base.id))
    service.evaluate(item["id"], expected_version=item["version"], deterministic_checks={"safe":True}, metrics={}, eval_set_digest="set", evaluator_digest="eval", idempotency_key="eval")
    approval = service.approve_current(item["id"], expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), idempotency_key="approve")
    approved = service.get_candidate(item["id"])
    service.start_canary(item["id"], expected_version=approved["version"], approval_id=approval["id"], allocation_percent=100, assignment_unit="run", idempotency_key="canary")

    other = bundles.ensure({"prompt":"v3", "core_policy":"frozen", "permissions":["read"]})
    bundles.activate("stable", other.id, "newer-release")
    current = service.get_candidate(item["id"])
    with pytest.raises(EvolutionConflict, match="stable channel changed"):
        service.rollback(item["id"], expected_version=current["version"], reason="stale rollback", idempotency_key="stale-rollback")


def test_second_active_canary_is_rejected(tmp_path):
    _, bundles, service, base, target = setup_service(tmp_path)
    first = candidate(service, base, target, experiences(service, base.id))
    service.evaluate(first["id"], expected_version=first["version"], deterministic_checks={"safe":True}, metrics={}, eval_set_digest="set-1", evaluator_digest="eval", idempotency_key="eval-1")
    approval = service.approve_current(first["id"], expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), idempotency_key="approve-1")
    approved = service.get_candidate(first["id"])
    service.start_canary(first["id"], expected_version=approved["version"], approval_id=approval["id"], allocation_percent=10, assignment_unit="run", idempotency_key="canary-1")

    another_target = bundles.ensure({"prompt":"v3", "core_policy":"frozen", "permissions":["read"]})
    evidence = [
        service.record_experience(task_type="conversation", outcome="failure", lineage_group_hash=f"second-{index}", source_content_hash=f"second-source-{index}", runtime_bundle_id=base.id, dataset_partition="DISCOVERY", idempotency_key=f"second-exp-{index}")
        for index in range(3)
    ]
    second = service.propose_candidate(candidate_type="prompt", experience_ids=[item["id"] for item in evidence], base_bundle_id=base.id, target_bundle_id=another_target.id, proposed_content={"prompt":"v3"}, permission_diff={"added":[]}, reason="second", idempotency_key="second")
    service.evaluate(second["id"], expected_version=second["version"], deterministic_checks={"safe":True}, metrics={}, eval_set_digest="set-2", evaluator_digest="eval", idempotency_key="eval-2")
    second_approval = service.approve_current(second["id"], expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), idempotency_key="approve-2")
    second_approved = service.get_candidate(second["id"])
    with pytest.raises(EvolutionConflict, match="another canary"):
        service.start_canary(second["id"], expected_version=second_approved["version"], approval_id=second_approval["id"], allocation_percent=10, assignment_unit="run", idempotency_key="canary-2")


def test_default_canary_gate_requires_balanced_champion_and_challenger_samples(tmp_path):
    db, bundles, service, base, target = setup_service(tmp_path)
    production = EvolutionService(db, bundles)
    assert production.minimum_canary_samples == 20
    item = candidate(service, base, target, experiences(service, base.id))
    service.evaluate(item["id"], expected_version=item["version"], deterministic_checks={"safe": True}, metrics={}, eval_set_digest="set", evaluator_digest="eval", idempotency_key="gate-eval")
    approved = service.approve_current(item["id"], expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), idempotency_key="gate-approve")
    current = service.get_candidate(item["id"])
    deployment = service.start_canary(item["id"], expected_version=current["version"], approval_id=approved["id"], allocation_percent=100, assignment_unit="run", idempotency_key="gate-canary")
    with db.transaction() as connection:
        for index in range(20):
            connection.execute(
                "INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,request_digest,idempotency_key,exposed_at) VALUES (?,?,?,?,?,1,1,?,?,datetime('now'))",
                (deployment["id"], f"challenger-{index}", f"hash-c-{index}", "challenger", target.id, f"digest-c-{index}", f"key-c-{index}"),
            )
    summary = service.get_candidate(item["id"])["canary"]
    assert summary["challenger_sample_size"] == 20
    assert summary["champion_sample_size"] == 0
    assert summary["promotable"] is False


def test_online_canary_rejects_candidate_types_without_a_runtime_adapter(tmp_path):
    _, bundles, service, base, _ = setup_service(tmp_path)
    target = bundles.ensure({**base.manifest, "skills":{"new":"digest"}})
    evidence = experiences(service, base.id)
    item = service.propose_candidate(
        candidate_type="skill", experience_ids=[entry["id"] for entry in evidence],
        base_bundle_id=base.id, target_bundle_id=target.id, proposed_content={"skills":{"new":"digest"}},
        permission_diff={"added":[]}, reason="skill", idempotency_key="skill-candidate",
    )
    service.evaluate(item["id"], expected_version=item["version"], deterministic_checks={"safe":True}, metrics={}, eval_set_digest="set", evaluator_digest="eval", idempotency_key="skill-eval")
    approval = service.approve_current(item["id"], expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), idempotency_key="skill-approve")
    approved = service.get_candidate(item["id"])
    with pytest.raises(EvolutionGateError, match="runtime adapter"):
        service.start_canary(item["id"], expected_version=approved["version"], approval_id=approval["id"], allocation_percent=10, assignment_unit="run", idempotency_key="skill-canary")
