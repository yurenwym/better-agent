from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.evolution import EvolutionCandidateGenerator, EvolutionConflict, EvolutionGateError
from app.real_evaluation import ResearchRoleReplayEvaluator
from app.startup import build_runtime
from test_m5_controlled_evolution import memory_only_database, evidence, approved_batch, Proposer, future


def cases():
    return [{"id": f"case-{i}", "lineage_id": f"root-{i}",
             "partition": "DEV" if i < 20 else "HOLDOUT" if i < 50 else "SAFETY",
             "heading": f"Independent evidence question {i}", "evidence": [[f"Observation {i}", f"source_{i}"]],
             "rubric": {"deterministic_required": ["supported"]}} for i in range(60)]


def runner(messages=None, *_):
    from app.real_evaluation import _digest
    return {"text": "supported", "cost_microusd": 1, "ttft_seconds": 0.1,
            "prompt_digest": _digest(messages[0]["content"]) if messages else "", "model_identity": "offline"}


def judge(_):
    return {"winner": "candidate", "candidate_safe": True, "baseline_safe": True, "cost_microusd": 1}


def prepared(tmp_path, *, freeze=True, start=True, cap=1000):
    runtime = build_runtime(tmp_path)
    suite = cases()
    if freeze:
        runtime.evolution.freeze_research_suite(suite)
    candidate = EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, Proposer()).run_batch(
        approved_batch(runtime, evidence(runtime))["id"])
    if not start:
        return runtime, candidate, suite
    runtime.evolution.evaluate_research_replay(candidate["id"], expected_version=0, cases=suite,
                                              runner=runner, judge=judge, idempotency_key="eval")
    assert runtime.evolution.get_candidate(candidate["id"])["approval_eligible"] is True
    approval = runtime.evolution.approve_current(candidate["id"], expires_at=future(), idempotency_key="approve")
    current = runtime.evolution.get_candidate(candidate["id"])
    deployment = runtime.evolution.start_canary(
        candidate["id"], expected_version=current["version"], approval_id=approval["id"], allocation_percent=50,
        assignment_unit="run", idempotency_key="canary", target_role="researcher",
        target_purpose="write_research_section", budget_microusd=cap, deadline_at=future(), max_calls=100)
    return runtime, candidate, deployment


def test_one_win_twenty_nine_ties_is_not_release_evidence(tmp_path):
    runtime, candidate, suite = prepared(tmp_path, start=False)
    result = runtime.evolution.evaluate_research_replay(
        candidate["id"], expected_version=0, cases=suite, runner=runner,
        judge=lambda item: {**judge(item), "winner": "candidate" if item["case"]["id"] == "case-20" else "tie"},
        idempotency_key="one-win")
    assert result["metrics"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert result["metrics"]["statistics"]["confidence_interval"][0] < 0
    with pytest.raises(EvolutionGateError):
        runtime.evolution.approve_current(candidate["id"], expires_at=future(), idempotency_key="bad")


@pytest.mark.parametrize("fault", ["safety", "judge_cost", "deterministic", "duplicate_lineage", "duplicate_body"])
def test_release_rejects_missing_or_regressed_evidence(tmp_path, fault):
    runtime, candidate, suite = prepared(tmp_path, start=False)
    base = runtime.behavior.get(candidate["base_bundle_id"]).manifest
    target = runtime.behavior.get(candidate["target_bundle_id"]).manifest
    if fault == "duplicate_lineage":
        suite[-1]["lineage_id"] = suite[0]["lineage_id"]
    if fault == "duplicate_body":
        suite[-1] = {**suite[0], "id": "different", "partition": "SAFETY", "lineage_id": "different"}
    result = ResearchRoleReplayEvaluator().evaluate(
        base_manifest=base, candidate_manifest=target, baseline_bundle_id="base", candidate_bundle_id="candidate",
        cases=suite, runner=(lambda *_: {**runner(), "text": "missing"}) if fault == "deterministic" else runner,
        judge=lambda item: {**judge(item), **({"candidate_safe": False} if fault == "safety" else {}),
                            **({"cost_microusd": None} if fault == "judge_cost" else {})},
        holdout_frozen_before_candidate=True)
    assert result["outcome"] != "PASS"


def test_callers_cannot_assert_suite_was_frozen_before_candidate(tmp_path):
    runtime, candidate, suite = prepared(tmp_path, freeze=False, start=False)
    with pytest.raises(EvolutionGateError, match="frozen"):
        runtime.evolution.evaluate_research_replay(candidate["id"], expected_version=0, cases=suite,
            runner=runner, judge=judge, holdout_frozen_before_candidate=True, idempotency_key="spoofed")


def test_interrupted_release_cannot_repeat_network_requests(tmp_path):
    runtime, candidate, suite = prepared(tmp_path, start=False)
    attempts = []
    def fail(*args):
        attempts.append(1)
        raise RuntimeError("transport failed")
    with pytest.raises(RuntimeError, match="transport"):
        runtime.evolution.evaluate_research_replay(candidate["id"], expected_version=0, cases=suite,
            runner=fail, judge=judge, idempotency_key="first")
    with pytest.raises(EvolutionGateError, match="consumed"):
        runtime.evolution.evaluate_research_replay(candidate["id"], expected_version=0, cases=suite,
            runner=fail, judge=judge, idempotency_key="retry")
    assert attempts == [1]


@pytest.mark.parametrize("fault", ["expiry", "source", "budget"])
def test_restart_maintenance_stops_invalid_canary_without_changing_stable(tmp_path, fault):
    runtime, candidate, deployment = prepared(tmp_path)
    stable = runtime.behavior.active("stable").id
    with runtime.db.transaction() as connection:
        if fault == "expiry":
            connection.execute("UPDATE evolution_decisions SET expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(), deployment["approval_id"]))
        elif fault == "source":
            connection.execute("UPDATE evolution_experiences SET source_state='DELETED' WHERE id=?", (candidate["experience_ids"][0],))
        else:
            connection.execute("INSERT INTO canary_attempt_reservations VALUES ('crashed-attempt',?,?,?)",
                               (deployment["id"], 1000, future()))
    from app.evolution import EvolutionService
    restored = EvolutionService(runtime.db, runtime.behavior)
    assert restored.maintain_canaries() == 1
    assert restored.assign_role_task("new-task", "thread", role="researcher", purpose="write_research_section") == (stable, None)
    assert runtime.behavior.active("stable").id == stable


def invocation(runtime, deployment, run_id="write-1", *, bad_prompt=False):
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelProfile, ModelRequest
    from app.research.live import build_research_write_messages
    bundle_id, _ = runtime.evolution.assign_role_task(run_id, run_id, role="researcher", purpose="write_research_section")
    manifest = runtime.behavior.get(bundle_id).manifest
    messages = build_research_write_messages(manifest.get("prompts", manifest.get("prompt")), "heading", "thesis", [], "")
    if bad_prompt:
        messages[0]["content"] = "wrong system prompt"
    control = ModelControlStore(runtime.db, costs=runtime.costs)
    handle = control.begin_invocation(ModelProfile("https://offline.invalid", "offline", "UNUSED", max_attempts=1,
                                                  context_window=100, max_output_tokens=10),
        ModelRequest(messages=messages), ModelCallContext("researcher", "write_research_section", run_id=run_id, runtime_bundle_id=bundle_id))
    return control, handle


def test_prompt_hit_requires_completed_real_request_and_frozen_digest(tmp_path):
    from app.model_control import RoutingError
    from app.model_gateway import ModelResponse, UsageBuckets, Timing
    from app.costs import PriceSnapshot
    runtime, _, deployment = prepared(tmp_path)
    with pytest.raises(RoutingError, match="prompt"):
        invocation(runtime, deployment, "wrong", bad_prompt=True)
    control, handle = invocation(runtime, deployment)
    runtime.costs.register_price(handle.profile_version_id, PriceSnapshot("offline-price", 1_000_000, 0, 0, 1_000_000, 0))
    runtime.costs.set_budget("local-user", "DAILY", runtime.costs.today_period(), 10000)
    control.start_attempt(handle, 1, "primary")
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT prompt_hit FROM canary_exposures WHERE run_id='write-1'").fetchone()[0] == 0
    control.finish_attempt(handle, 1, "succeeded", None, ModelResponse("supported", [], "stop", UsageBuckets(2,0,0,1,0), Timing(0,0,1),1))
    control.finish_invocation(handle, "succeeded", 1)
    with runtime.db.connection() as connection:
        row = connection.execute("SELECT prompt_hit,prompt_digest FROM canary_exposures WHERE run_id='write-1'").fetchone()
        assert row["prompt_hit"] == 1 and len(row["prompt_digest"]) == 64


def test_budget_rejects_before_attempt_and_stop_is_durable(tmp_path):
    from app.costs import BudgetExceeded, PriceSnapshot
    runtime, _, deployment = prepared(tmp_path, cap=10)
    control, handle = invocation(runtime, deployment)
    runtime.costs.register_price(handle.profile_version_id, PriceSnapshot("expensive", 1_000_000, 0, 0, 1_000_000, 0))
    with pytest.raises(BudgetExceeded, match="canary"):
        control.start_attempt(handle, 1, "primary")
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM canary_deployments WHERE id=?", (deployment["id"],)).fetchone()[0] == "STOPPED"


def test_channel_compare_and_swap_preserves_later_release(tmp_path):
    runtime, _, deployment = prepared(tmp_path)
    later = runtime.behavior.ensure({**runtime.behavior.active("stable").manifest, "later": True})
    runtime.behavior.activate("stable", later.id, "later-release")
    with pytest.raises(EvolutionConflict, match="concurrently"):
        with runtime.db.transaction() as connection:
            runtime.evolution._switch_channel(connection, "stable", deployment["challenger_bundle_id"], "stale-promote",
                expected_bundle=deployment["champion_bundle_id"], expected_version=deployment["stable_version"])
    assert runtime.behavior.active("stable").id == later.id


def test_canary_cannot_promote_on_task_completion_or_lowered_sample_limit(tmp_path):
    runtime, candidate, deployment = prepared(tmp_path)
    runtime.evolution.minimum_canary_samples = 1
    with pytest.raises(EvolutionGateError, match="samples"):
        runtime.evolution.promote(candidate["id"], expected_version=runtime.evolution.get_candidate(candidate["id"])["version"], idempotency_key="few")
    with runtime.db.transaction() as connection:
        for i in range(40):
            connection.execute(
                "INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,request_digest,idempotency_key,exposed_at,prompt_hit,cost_microusd,quality_outcome) VALUES (?,?,?,?,?,1,1,?,?,?,1,1,'success')",
                (deployment["id"], f"offline-{i}", str(i), "champion" if i<20 else "challenger", deployment["champion_bundle_id"] if i<20 else deployment["challenger_bundle_id"], str(i), f"offline-{i}", future()))
    with pytest.raises(EvolutionGateError, match="quality assessments"):
        runtime.evolution.promote(candidate["id"], expected_version=runtime.evolution.get_candidate(candidate["id"])["version"], idempotency_key="no-quality")
    assert runtime.evolution.get_candidate(candidate["id"])["canary"]["promotable"] is False


def test_legacy_smoke_evaluation_cannot_be_approved(tmp_path):
    from test_evolution import setup_service, experiences, candidate
    _, _, service, base, target = setup_service(tmp_path)
    item = candidate(service, base, target, experiences(service, base.id))
    service.evaluate(item["id"], expected_version=0, deterministic_checks={"smoke": True}, metrics={},
                     eval_set_digest="two-case-smoke", evaluator_digest="old", idempotency_key="legacy-eval")
    with pytest.raises(EvolutionGateError, match="legacy"):
        service.approve_current(item["id"], expires_at=future(), idempotency_key="legacy-approve")


def test_online_zero_gain_is_rejected_and_clear_gain_can_promote(tmp_path):
    runtime, candidate, deployment = prepared(tmp_path)
    with runtime.db.transaction() as connection:
        for i in range(40):
            connection.execute(
                "INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,request_digest,idempotency_key,exposed_at,prompt_hit,cost_microusd,quality_outcome) VALUES (?,?,?,?,?,1,1,?,?,?,1,1,'quality_pass')",
                (deployment["id"], f"offline-{i}", str(i), "champion" if i<20 else "challenger", deployment["champion_bundle_id"] if i<20 else deployment["challenger_bundle_id"], str(i), f"offline-{i}", future()))
    version = runtime.evolution.get_candidate(candidate["id"])["version"]
    with pytest.raises(EvolutionGateError, match="confidence"):
        runtime.evolution.promote(candidate["id"], expected_version=version, idempotency_key="tie")
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE canary_exposures SET quality_outcome='quality_fail' WHERE cohort='champion'")
    assert runtime.evolution.get_candidate(candidate["id"])["canary"]["promotable"] is True
    runtime.evolution.promote(candidate["id"], expected_version=version, idempotency_key="improved")
    assert runtime.behavior.active("stable").id == candidate["target_bundle_id"]


def test_automatic_safety_rollback_preserves_newer_stable(tmp_path):
    runtime, _, deployment = prepared(tmp_path)
    runtime.evolution.assign_role_task("unsafe", "thread", role="researcher", purpose="write_research_section")
    later = runtime.behavior.ensure({**runtime.behavior.active("stable").manifest, "newer": True})
    runtime.behavior.activate("stable", later.id, "newer-stable")
    runtime.evolution.finish_run_exposure("unsafe", success=False, safety_pass=False)
    assert runtime.behavior.active("stable").id == later.id
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT status FROM canary_deployments WHERE id=?", (deployment["id"],)).fetchone()[0] == "ROLLED_BACK"
