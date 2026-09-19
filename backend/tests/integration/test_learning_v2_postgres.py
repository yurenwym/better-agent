import asyncio

import pytest

from app.startup import build_runtime
from app.learning import LearningConflict
from test_learning_v2 import feedback, compile_plan


@pytest.fixture
def learning_runtime(tmp_path, migrated_postgres_url, monkeypatch):
    for name in ("AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)
    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory"],
                               cycle_microusd=100, daily_microusd=200, monthly_microusd=1000)
    yield runtime
    runtime.db._pool.close()


def test_postgres_memory_to_new_plan_and_correction(learning_runtime):
    runtime = learning_runtime
    feedback(runtime, "以后每次练习最多30分钟")
    first = compile_plan(runtime)
    assert first["structure"]["actions"][0]["estimated_minutes"] == 30
    feedback(runtime, "以后每次练习最多20分钟")
    second = compile_plan(runtime, "second")
    assert second["structure"]["actions"][0]["estimated_minutes"] == 20
    assert runtime.learning.history()[0]["status"] == "APPLIED"


def test_postgres_independent_learning_budget_and_unknown_recovery(learning_runtime):
    runtime = learning_runtime
    runtime.costs.set_budget("local-user", "DAILY", runtime.costs.today_period(), 10000)
    runtime.costs.set_budget("local-user", "MONTHLY", runtime.costs.today_period()[:7], 100000)
    runtime.learning.enqueue("local-user", "experience", "event", "hash", "expired-source-root")
    job = runtime.learning.claim()
    root_id = runtime.learning.dispatch(job)
    with runtime.db.connection() as connection:
        root = connection.execute("SELECT * FROM task_budget_roots WHERE id=?", (root_id,)).fetchone()
        assert root["root_kind"] == "learning" and root["root_object_id"] == job["id"]
    with pytest.raises(LearningConflict, match="already dispatched"):
        runtime.learning.dispatch(job)
    runtime.learning.enqueue("local-user", "experience", "event-2", "hash", "root-2")
    next_job = runtime.learning.claim()
    runtime.learning.dispatch(next_job)
    runtime.learning.enqueue("local-user", "experience", "event-3", "hash", "root-3")
    excess = runtime.learning.claim()
    with pytest.raises(LearningConflict, match="aggregate budget"):
        runtime.learning.dispatch(excess)
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE learning_jobs SET lease_until='2000-01-01' WHERE id=?", (job["id"],))
    assert runtime.learning.claim() is None
    assert next(item for item in runtime.learning.history() if item["id"] == job["id"])["status"] == "UNKNOWN"


def test_postgres_claim_cas_and_policy_pause(learning_runtime):
    from concurrent.futures import ThreadPoolExecutor
    runtime = learning_runtime
    runtime.learning.enqueue("local-user", "experience", "event", "hash", "root")
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: runtime.learning.claim(), range(2)))
    assert sum(item is not None for item in results) == 1
    runtime.learning.configure("local-user", expected_version=1, paused=True, allowed_assets=["memory"])
    with pytest.raises(LearningConflict, match="authority"):
        runtime.learning.dispatch(next(item for item in results if item))


def test_postgres_workflow_adoption_and_revocation(learning_runtime):
    from test_learning_v2 import completed_program
    runtime = learning_runtime
    runtime.learning.configure("local-user", expected_version=1, paused=False, allowed_assets=["skill"])
    completed_program(runtime, "english-workflow", method=True)
    runtime.learning.run_once()
    completed_program(runtime, "math-workflow", method=True)
    runtime.learning.run_once()
    job = next(item for item in runtime.learning.history() if item["status"] == "APPLIED")
    matches = runtime.learning.matching_skills("local-user", None, "安排复习")
    assert [item["version_id"] for item in matches] == [job["changes"][0]["after"]]
    runtime.learning.suspend(job["id"], "local-user", expected_version=job["version"], reason="user_requested")
    assert not runtime.learning.matching_skills("local-user", None, "安排复习")


@pytest.mark.parametrize("fail_at", [None, 2])
def test_postgres_prompt_cycle_uses_real_ledger_and_request_snapshots(tmp_path, migrated_postgres_url, monkeypatch, fail_at):
    import json
    from app.model_gateway import ModelProfile, ModelResponse, UsageBuckets, Timing
    from app.costs import PriceSnapshot
    from test_m5_release_gates import cases
    from test_m5_controlled_evolution import evidence
    for key in ("AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "text,streaming,tool_calling,json_object,json_schema")
    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url, profile=ModelProfile("https://offline.invalid", "fixture", "UNUSED", max_attempts=1))
    gateway = runtime.learning.prompt_learning.gateway
    calls = []
    async def execute(profile, request, **kwargs):
        calls.append(request.purpose)
        if len(calls) == fail_at:
            from app.model_gateway import GatewayError
            raise GatewayError("response lost after send", "transport")
        if request.purpose == "propose_evolution_candidate":
            body = json.dumps({"prompt": "Use supported evidence only. improved", "reason": "evidence", "root_cause_hypothesis": "behavior", "confidence_limitations": "offline control test"})
        elif request.purpose == "write_research_section":
            body = "supported improved" if "improved" in request.messages[0]["content"] else "supported"
        elif request.purpose == "judge_learning_quality":
            data = json.loads(request.messages[1]["content"])
            body = json.dumps({"winner": "left" if "improved" in data["left"] else "right"})
        else:
            body = '{"left_safe":true,"right_safe":true}'
        return ModelResponse(body, [], "stop", UsageBuckets(10, 0, 0, 10, 0), Timing(1, 1.1, 1.2), 1)
    gateway._execute_attempt = execute
    profile_id = runtime.behavior.active("stable").manifest["model_role_bindings"]["researcher"]["primary"]
    runtime.costs.register_price(profile_id, PriceSnapshot("learning-fixture-price", 100, 0, 0, 100, 0))
    runtime.costs.set_budget("local-user", "DAILY", runtime.costs.today_period(), 1000000)
    runtime.costs.set_budget("local-user", "MONTHLY", runtime.costs.today_period()[:7], 1000000)
    evidence(runtime)
    suite = runtime.learning.prompt_learning.register_suite("local-user", cases())
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["prompt"], max_attempts=241,
                               cycle_microusd=100000, daily_microusd=100000, monthly_microusd=100000, prompt_suite_digest=suite)
    try:
        runtime.learning.run_once()
        job = runtime.learning.history()[0]
        if fail_at:
            assert job["status"] == "UNKNOWN", job["reason"]
            assert len(calls) == fail_at
            assert not runtime.learning.run_once()
            assert len(calls) == fail_at
            return
        assert job["status"] == "NO_CHANGE", job["reason"]
        assert len(calls) == 241
        with runtime.db.connection() as connection:
            root = connection.execute("SELECT attempts_started FROM task_budget_roots WHERE id=?", (job["root_budget_id"],)).fetchone()
            assert root[0] == 241
            assert connection.execute("SELECT COUNT(*) FROM learning_snapshots WHERE task_kind='invocation'").fetchone()[0] == 241
        runtime.learning.run_once()
        assert any(job["status"] == "APPLIED" for job in runtime.learning.history())
        runtime.learning.run_once()
        assert len(calls) == 241
    finally:
        runtime.db.close()
