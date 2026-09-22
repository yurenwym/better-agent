"""Promotion Gate: harness rules, per-target apply paths, append-only audit."""
import sqlite3

import pytest

from app.learning_contract import TARGET_BEHAVIOR, TARGET_MEMORY, TARGET_SKILL
from app.learning_eval import EvaluationResult
from app.learning_promotion import (
    DEFAULT_PROMOTION_POLICY,
    GATE_CHECKS,
    LearningPromotionGate,
    OUTCOME_CANARY_PENDING,
    OUTCOME_NEEDS_REPLAY,
    OUTCOME_PENDING_APPROVAL,
    OUTCOME_PENDING_RELEASE_EVALUATION,
    OUTCOME_PROMOTED,
    OUTCOME_REJECTED,
    OUTCOME_ROLLED_BACK,
    OUTCOME_SHADOW,
    PromotionPolicy,
    cost_increase,
    promotion_gate,
)
from app.learning_targets import build_adapters
from app.startup import build_runtime

from tests.test_learning_targets import (
    behavior_draft,
    memory_draft,
    record_experiences,
    runtime_with_message,
    skill_draft,
)


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)


def evaluation(target=TARGET_MEMORY, *, outcome="PASS", rule_pass=True, checks=None, judge=None, metrics=None):
    return EvaluationResult(
        target=target, candidate_id="cand_1", rule_pass=rule_pass,
        rule_checks=checks if checks is not None else {"no_secret": True},
        judge=judge, outcome=outcome, reason="", metrics=metrics or {},
    )


def gate_for(runtime, *, policy=DEFAULT_PROMOTION_POLICY):
    return LearningPromotionGate(
        build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                       bundles=runtime.behavior, evolution=runtime.evolution),
        policy=policy,
    )


# ------------------------------------------------------------- gate is rules

def test_gate_passes_a_clean_rule_only_memory_evaluation():
    checks = promotion_gate(evaluation(TARGET_MEMORY))
    assert checks["rule_eval_pass"] and checks["judge_not_worse"]
    assert all(checks[name] for name in GATE_CHECKS), checks


def test_gate_fails_when_the_rule_evaluation_failed():
    checks = promotion_gate(evaluation(TARGET_MEMORY, outcome="FAIL", rule_pass=False))
    assert checks["rule_eval_pass"] is False


def test_gate_reports_needs_replay_as_incomplete_evidence():
    checks = promotion_gate(evaluation(TARGET_SKILL, outcome="NEEDS_REPLAY", judge=None))
    assert checks["evidence_complete"] is False
    assert checks["judge_present"] is False


def test_gate_demands_a_judge_for_skill_and_behavior():
    no_judge = promotion_gate(evaluation(TARGET_SKILL, judge=None))
    assert no_judge["judge_present"] is False
    with_judge = promotion_gate(evaluation(TARGET_SKILL, judge={"wins": 3, "losses": 0, "ties": 1}))
    assert with_judge["judge_present"] is True and with_judge["judge_not_worse"] is True


def test_gate_refuses_a_candidate_the_judge_scored_worse():
    checks = promotion_gate(evaluation(TARGET_SKILL, judge={"wins": 1, "losses": 4, "ties": 0}))
    assert checks["judge_not_worse"] is False


def test_gate_treats_a_tie_as_not_worse():
    checks = promotion_gate(evaluation(TARGET_SKILL, judge={"wins": 2, "losses": 2, "ties": 0}))
    assert checks["judge_not_worse"] is True


def test_gate_refuses_any_safety_regression():
    checks = promotion_gate(evaluation(TARGET_BEHAVIOR, judge={"wins": 5, "losses": 0},
                                       metrics={"safety_regressions": 1}))
    assert checks["no_safety_regression"] is False


def test_gate_refuses_permission_expansion_from_rules_or_metrics():
    from_rules = promotion_gate(evaluation(TARGET_SKILL, judge={"wins": 1, "losses": 0},
                                           checks={"no_requested_tools": False}))
    assert from_rules["no_permission_expansion"] is False
    from_metrics = promotion_gate(evaluation(TARGET_BEHAVIOR, judge={"wins": 1, "losses": 0},
                                             metrics={"permission_expansion": True}))
    assert from_metrics["no_permission_expansion"] is False


def test_gate_ignores_cost_when_no_limit_is_configured():
    metrics = {"cost_baseline_microusd": 100, "cost_candidate_microusd": 900}
    assert promotion_gate(evaluation(TARGET_MEMORY, metrics=metrics))["cost_within_limit"] is True
    assert cost_increase(metrics) == 8.0


def test_gate_enforces_the_configured_cost_limit():
    policy = PromotionPolicy(cost_increase_limit=0.2)
    over = promotion_gate(evaluation(TARGET_MEMORY, metrics={"cost_baseline_microusd": 100,
                                                            "cost_candidate_microusd": 150}), policy=policy)
    assert over["cost_within_limit"] is False
    under = promotion_gate(evaluation(TARGET_MEMORY, metrics={"cost_baseline_microusd": 100,
                                                             "cost_candidate_microusd": 110}), policy=policy)
    assert under["cost_within_limit"] is True


def test_gate_fails_closed_when_a_limit_is_set_but_nothing_was_measured():
    policy = PromotionPolicy(cost_increase_limit=0.2)
    checks = promotion_gate(evaluation(TARGET_MEMORY, metrics={}), policy=policy)
    assert checks["cost_within_limit"] is False


def test_gate_is_a_pure_function_of_the_evaluation():
    first = promotion_gate(evaluation(TARGET_SKILL, judge={"wins": 2, "losses": 1, "ties": 1}))
    second = promotion_gate(evaluation(TARGET_SKILL, judge={"wins": 2, "losses": 1, "ties": 1}))
    assert first == second and set(first) == set(GATE_CHECKS)


# -------------------------------------------------------------- Memory path

def test_memory_promotion_accepts_the_proposal_and_records_it(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    gate = gate_for(runtime)
    decision = gate.promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user", job_id="job_memory")
    assert decision.outcome == OUTCOME_PROMOTED, decision.reason
    assert decision.promoted is True
    assert decision.detail["applied"]["status"] == "ACCEPTED"
    assert len(runtime.memory_store.list_entries()) == 1
    assert [row["outcome"] for row in gate.history("local-user")] == [OUTCOME_PROMOTED]


def test_memory_promotion_needs_no_approval(tmp_path):
    assert TARGET_MEMORY in DEFAULT_PROMOTION_POLICY.auto_promote
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    decision = gate_for(runtime).promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user")
    assert decision.outcome == OUTCOME_PROMOTED


# --------------------------------------------------------------- Skill path

def skill_candidate(runtime):
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["skill"])
    job_id = runtime.learning.enqueue("local-user", "experience", "e", "hash", "root")
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_SKILL]
    return adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=job_id)


def test_skill_promotion_waits_for_approval_and_changes_nothing(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    decision = gate.promote(candidate, evaluation(TARGET_SKILL, judge={"wins": 4, "losses": 1, "ties": 0}),
                            owner_id="local-user", job_id="job_skill")
    assert decision.outcome == OUTCOME_PENDING_APPROVAL
    assert runtime.skill_platform.version(candidate["version_id"])["status"] != "ENABLED"
    assert gate.history("local-user")[-1]["outcome"] == OUTCOME_PENDING_APPROVAL


def test_skill_promotion_enables_without_granting_authority(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    decision = gate.promote(candidate, evaluation(TARGET_SKILL, judge={"wins": 4, "losses": 1, "ties": 0}),
                            owner_id="local-user", approval={"actor": "user"}, job_id="job_skill")
    assert decision.outcome == OUTCOME_PROMOTED, decision.reason
    assert decision.detail["applied"]["granted_tools"] == []
    version = runtime.skill_platform.version(candidate["version_id"])
    assert version["status"] == "ENABLED" and version["granted_tools"] == []


def test_skill_promotion_is_refused_by_a_losing_judge(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    decision = gate.promote(candidate, evaluation(TARGET_SKILL, judge={"wins": 1, "losses": 3, "ties": 0}),
                            owner_id="local-user", approval={"actor": "user"})
    assert decision.outcome == OUTCOME_REJECTED
    assert "judge_not_worse" in decision.reason
    assert runtime.skill_platform.version(candidate["version_id"])["status"] != "ENABLED"


def test_a_rejected_gate_leaves_no_audit_row_claiming_promotion(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    gate.promote(candidate, evaluation(TARGET_SKILL, outcome="FAIL", rule_pass=False),
                 owner_id="local-user", approval={"actor": "user"})
    assert gate.metrics("local-user")["promoted"] == 0


# ------------------------------------------------------------ Behavior path

def behavior_candidate(runtime):
    items = record_experiences(runtime)
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_BEHAVIOR]
    return adapter.create_candidate(behavior_draft([item["id"] for item in items]),
                                    owner_id="local-user", job_id="job_behavior")


def test_behavior_promotion_waits_for_approval_before_any_canary(tmp_path):
    runtime = build_runtime(tmp_path)
    candidate = behavior_candidate(runtime)
    gate = gate_for(runtime)
    decision = gate.promote(candidate, evaluation(TARGET_BEHAVIOR, judge={"wins": 3, "losses": 1, "ties": 0},
                                                  metrics={"safety_regressions": 0}),
                            owner_id="local-user", job_id="job_behavior")
    assert decision.outcome == OUTCOME_PENDING_APPROVAL
    assert runtime.evolution.get_candidate(candidate["candidate_id"], "local-user")["status"] == "READY_FOR_EVAL"
    assert gate.history("local-user")[-1]["outcome"] == OUTCOME_PENDING_APPROVAL


def test_behavior_promotion_demands_an_approvable_release_evaluation_first(tmp_path):
    """This build only approves prompt candidates through the M5 release contract."""
    runtime = build_runtime(tmp_path)
    candidate = behavior_candidate(runtime)
    decision = gate_for(runtime).promote(
        candidate, evaluation(TARGET_BEHAVIOR, judge={"wins": 3, "losses": 1, "ties": 0}),
        owner_id="local-user", approval={"actor": "user"})
    assert decision.outcome == OUTCOME_PENDING_RELEASE_EVALUATION
    assert runtime.evolution.get_candidate(candidate["candidate_id"], "local-user")["status"] == "READY_FOR_EVAL"


def test_behavior_canary_cannot_start_without_enough_exposures(tmp_path):
    runtime = build_runtime(tmp_path)
    candidate = behavior_candidate(runtime)
    gate = gate_for(runtime)
    status = gate.canary_status(candidate, owner_id="local-user")
    assert status["ready"] is False
    decision = gate.complete_canary(candidate, owner_id="local-user")
    assert decision.outcome == OUTCOME_CANARY_PENDING
    assert decision.outcome not in {OUTCOME_PROMOTED}


# ------------------------------------------------------- idempotency + audit

def test_promoting_the_same_memory_candidate_twice_records_one_promotion(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    gate = gate_for(runtime)
    first = gate.promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user", job_id="job_memory")
    second = gate.promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user", job_id="job_memory")
    assert first.outcome == second.outcome == OUTCOME_PROMOTED
    assert gate.metrics("local-user")["promoted"] == 1
    assert len(runtime.memory_store.list_entries()) == 1


def test_the_database_refuses_a_second_promotion_row_for_one_candidate(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    gate = gate_for(runtime)
    gate.promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user")
    with pytest.raises(sqlite3.IntegrityError):
        with runtime.db.transaction() as connection:
            connection.execute(
                "INSERT INTO learning_promotions(id,owner_id,job_id,target,candidate_id,evaluation_id,outcome,"
                "gate_json,reason,detail_json,evidence_digest,idempotency_key,created_at) "
                "VALUES ('promotion_manual','local-user','','MEMORY',?,'','PROMOTED','{}','','{}','d','k','t')",
                (candidate["candidate_id"],),
            )


def test_the_promotion_audit_is_append_only(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    gate = gate_for(runtime)
    promotion_id = gate.promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user").promotion_id
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE learning_promotions SET reason='rewritten' WHERE id=?", (promotion_id,))


def test_needs_replay_is_recorded_without_touching_production(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    decision = gate.promote(candidate, evaluation(TARGET_SKILL, outcome="NEEDS_REPLAY"),
                            owner_id="local-user", approval={"actor": "user"})
    assert decision.outcome == OUTCOME_NEEDS_REPLAY
    assert runtime.skill_platform.version(candidate["version_id"])["status"] != "ENABLED"
    assert gate.history("local-user")[-1]["outcome"] == OUTCOME_NEEDS_REPLAY


# ------------------------------------------------------------ rollback + ops

def test_rollback_disables_a_promoted_skill_and_is_recorded(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    gate.promote(candidate, evaluation(TARGET_SKILL, judge={"wins": 4, "losses": 0, "ties": 0}),
                 owner_id="local-user", approval={"actor": "user"})
    decision = gate.rollback(candidate, owner_id="local-user", reason="regression in production")
    assert decision.outcome == OUTCOME_ROLLED_BACK
    assert runtime.skill_platform.version(candidate["version_id"])["status"] != "ENABLED"


def test_promotion_regression_rate_is_reported(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    gate.promote(candidate, evaluation(TARGET_SKILL, judge={"wins": 4, "losses": 0, "ties": 0}),
                 owner_id="local-user", approval={"actor": "user"})
    gate.rollback(candidate, owner_id="local-user", reason="regression")
    metrics = gate.metrics("local-user")
    assert metrics["promoted"] == 1 and metrics["rolled_back"] == 1
    assert metrics["promotion_regression_rate"] == 1.0
    assert metrics["candidate_promote_rate"] == 1.0


def test_gate_never_calls_a_model(tmp_path):
    """The gate is harness rules: no gateway is constructed, so no LLM can be reached."""
    runtime = build_runtime(tmp_path)
    gate = gate_for(runtime)
    assert not hasattr(gate, "gateway")
    assert not hasattr(gate, "judge")


# ------------------------------------------------------------- shadow mode

def test_shadow_mode_records_the_decision_without_applying_it(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    gate = gate_for(runtime)

    decision = gate.promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user", shadow=True)

    assert decision.outcome == OUTCOME_SHADOW
    assert decision.detail["would_be"] == OUTCOME_PROMOTED
    assert decision.promoted is False
    assert runtime.memory_store.list_entries() == []
    assert gate.metrics("local-user")["promoted"] == 0


def test_shadow_mode_still_reports_a_gate_failure_as_a_refusal(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    decision = gate.promote(candidate, evaluation(TARGET_SKILL, judge={"wins": 0, "losses": 5, "ties": 0}),
                            owner_id="local-user", approval={"actor": "user"}, shadow=True)
    assert decision.outcome == OUTCOME_REJECTED
    assert runtime.skill_platform.version(candidate["version_id"])["status"] != "ENABLED"


def test_shadow_mode_predicts_pending_approval_for_a_skill(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    candidate = skill_candidate(runtime)
    gate = gate_for(runtime)
    decision = gate.promote(candidate, evaluation(TARGET_SKILL, judge={"wins": 4, "losses": 0, "ties": 0}),
                            owner_id="local-user", shadow=True)
    assert decision.outcome == OUTCOME_SHADOW
    assert decision.detail["would_be"] == OUTCOME_PENDING_APPROVAL


def test_a_shadow_run_does_not_block_a_later_real_promotion(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                             bundles=runtime.behavior, evolution=runtime.evolution)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    gate = gate_for(runtime)
    gate.promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user", shadow=True)

    real = gate.promote(candidate, evaluation(TARGET_MEMORY), owner_id="local-user")

    assert real.outcome == OUTCOME_PROMOTED
    assert len(runtime.memory_store.list_entries()) == 1
    assert gate.metrics("local-user")["promoted"] == 1


def test_unknown_target_cannot_be_promoted(tmp_path):
    """Only the three contracted targets exist; anything else is a bypass attempt."""
    runtime = build_runtime(tmp_path)
    gate = gate_for(runtime)
    with pytest.raises(ValueError, match="unknown promotion target"):
        gate.promote({"candidate_id": "cand_x", "target": "EVOLUTION"},
                     evaluation("EVOLUTION"), owner_id="local-user")
    assert gate.history("local-user") == []
