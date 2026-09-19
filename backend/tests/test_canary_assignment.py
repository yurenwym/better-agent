"""Canary control-flow checks using the current frozen researcher release contract."""
import pytest
from app.evolution import EvolutionGateError
from test_m5_release_gates import prepared, invocation


def selected(runtime, task="research-task"):
    return runtime.evolution.assign_role_task(task, task, role="researcher", purpose="write_research_section")


def test_matching_role_is_pinned_without_inventing_a_safety_verdict(tmp_path):
    runtime, candidate, deployment = prepared(tmp_path)
    bundle, deployment_id = selected(runtime)
    assert deployment_id == deployment["id"]
    assert selected(runtime) == (bundle, deployment_id)
    with runtime.db.connection() as connection:
        row = connection.execute("SELECT success,safety_pass,prompt_hit FROM canary_exposures WHERE run_id='research-task'").fetchone()
    assert tuple(row) == (None, None, 0)
    assert runtime.evolution.assign_role_task("unrelated", "thread", role="planner", purpose="compile_goal_program") == (candidate["base_bundle_id"], None)


def test_canary_without_safety_evidence_cannot_promote(tmp_path):
    runtime, candidate, deployment = prepared(tmp_path)
    selected(runtime)
    runtime.evolution.finish_run_exposure("research-task", success=True, safety_pass=None)
    current = runtime.evolution.get_candidate(candidate["id"])
    with pytest.raises(EvolutionGateError):
        runtime.evolution.promote(candidate["id"], expected_version=current["version"], idempotency_key="premature")
    assert runtime.behavior.active("stable").id == candidate["base_bundle_id"]


def test_stopped_canary_resolves_to_stable(tmp_path):
    runtime, candidate, deployment = prepared(tmp_path)
    selected(runtime)
    runtime.evolution.finish_run_exposure("research-task", success=False, safety_pass=None)
    assert selected(runtime, "later") == (candidate["base_bundle_id"], None)


def test_safety_failure_rolls_back_with_model_ledger_metrics(tmp_path):
    runtime, candidate, deployment = prepared(tmp_path)
    invocation(runtime, deployment, "research-task")
    runtime.evolution.finish_run_exposure("research-task", success=False, safety_pass=False)
    with runtime.db.connection() as connection:
        row = connection.execute("SELECT invocation_count,safety_outcome FROM canary_exposures WHERE run_id='research-task'").fetchone()
    assert row["invocation_count"] == 1
    assert row["safety_outcome"] == "failed"
    assert runtime.evolution.get_candidate(candidate["id"])["status"] == "ROLLED_BACK"
    assert runtime.behavior.active("stable").id == candidate["base_bundle_id"]


def test_safety_rollback_is_atomic_with_exposure_metrics(tmp_path, monkeypatch):
    runtime, candidate, deployment = prepared(tmp_path)
    selected(runtime)
    def fail(*args, **kwargs):
        raise RuntimeError("event write failed")
    monkeypatch.setattr(runtime.evolution, "_event", fail)
    with pytest.raises(RuntimeError, match="event write failed"):
        runtime.evolution.finish_run_exposure("research-task", success=False, safety_pass=False)
    with runtime.db.connection() as connection:
        row = connection.execute("SELECT success,safety_pass FROM canary_exposures WHERE run_id='research-task'").fetchone()
    assert tuple(row) == (None, None)
    assert runtime.evolution.get_candidate(candidate["id"])["status"] == "CANARY"


def test_finished_exposure_is_frozen_on_replay(tmp_path):
    runtime, candidate, deployment = prepared(tmp_path)
    selected(runtime)
    runtime.evolution.finish_run_exposure("research-task", success=True, safety_pass=True)
    runtime.evolution.finish_run_exposure("research-task", success=False, safety_pass=False)
    with runtime.db.connection() as connection:
        row = connection.execute("SELECT success,safety_pass FROM canary_exposures WHERE run_id='research-task'").fetchone()
    assert tuple(row) == (1, 1)
    assert runtime.evolution.get_candidate(candidate["id"])["status"] == "CANARY"
