"""Regressions for the runtime gaps found in the V3 optimization review."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.learning_contract import TARGET_SKILL
from app.learning_decision import LearningDecision
from app.learning_pipeline import build_pipeline
from app.learning_replay import RuntimeLearningReplay
from app.learning_sources import user_sources
from app.skill_platform import SkillCandidateRejected
from tests.test_learning_pipeline_v3 import prepare, stub_budget, StubAgent, StubDecisions, enqueue
from tests.test_learning_targets import adapters, skill_draft, memory_draft


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH",
                 "BETTER_AGENT_LEARNING_V3", "BETTER_AGENT_LEARNING_REPLAY_FILE"):
        monkeypatch.delenv(name, raising=False)


def skill_job(runtime, key):
    return runtime.learning.enqueue("local-user", "experience", key, key, key)


def test_skill_versions_reuse_content_and_preserve_job_binding(tmp_path):
    runtime, _ = prepare(tmp_path)
    adapter = adapters(runtime)[TARGET_SKILL]
    first_job = skill_job(runtime, "first")
    first = adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=first_job)
    reused = adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=skill_job(runtime, "same"))
    assert first["version_id"] == reused["version_id"]
    changed = skill_draft(change={"content": skill_draft().proposed_change["content"] + "\n记录证据。"})
    second = adapter.create_candidate(changed, owner_id="local-user", job_id=skill_job(runtime, "second"))
    assert second["version_id"] != first["version_id"]
    assert second["version"] != first["version"]
    with pytest.raises(SkillCandidateRejected, match="bound"):
        adapter.create_candidate(changed, owner_id="local-user", job_id=first_job)
    assert all(item["name"] != first["name"] for item in runtime.skill_platform.enabled_versions())


def test_concurrent_skill_candidates_share_one_version(tmp_path):
    runtime, _ = prepare(tmp_path)
    jobs = [skill_job(runtime, str(i)) for i in range(3)]
    adapter = adapters(runtime)[TARGET_SKILL]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda job: adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=job), jobs))
    assert len({result["version_id"] for result in results}) == 1


def test_skill_rollback_restores_previous_default(tmp_path):
    runtime, _ = prepare(tmp_path)
    adapter = adapters(runtime)[TARGET_SKILL]
    first = adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=skill_job(runtime, "first"))
    adapter.promote(first, owner_id="local-user")
    second = adapter.create_candidate(skill_draft(change={"description": "new"}), owner_id="local-user", job_id=skill_job(runtime, "second"))
    adapter.promote(second, owner_id="local-user")
    adapter.rollback(second, owner_id="local-user")
    adapter.rollback(second, owner_id="local-user")
    assert runtime.skill_platform.default_version(first["name"])["version_id"] == first["version_id"]


def test_skill_rejects_a_changed_champion(tmp_path):
    runtime, _ = prepare(tmp_path)
    adapter = adapters(runtime)[TARGET_SKILL]
    first = adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=skill_job(runtime, "a"))
    second = adapter.create_candidate(skill_draft(change={"description": "new"}), owner_id="local-user", job_id=skill_job(runtime, "b"))
    adapter.promote(first, owner_id="local-user")
    with pytest.raises(ValueError, match="baseline changed"):
        adapter.promote(second, owner_id="local-user")
    assert runtime.skill_platform.default_version(first["name"])["version_id"] == first["version_id"]


def skill_pipeline(runtime, monkeypatch, **kwargs):
    stub_budget(monkeypatch, runtime)
    decision = LearningDecision(learn=True, target="SKILL", subtype="", confidence=1, support=1,
                               importance=.8, risk="low", reason_codes=(), model_identity="test")
    return build_pipeline(runtime, decisions=StubDecisions(runtime.db, decision),
                          agent=StubAgent(lambda *args: skill_draft()), **kwargs)


@pytest.mark.parametrize("error,status", [(SkillCandidateRejected("preflight"), "REJECTED"), (OSError("partial write"), "UNKNOWN")])
def test_candidate_failure_classification(tmp_path, monkeypatch, error, status):
    runtime, message = prepare(tmp_path)
    pipeline = skill_pipeline(runtime, monkeypatch)
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(runtime.skill_platform, "store_candidate", fail)
    enqueue(runtime, message)
    assert pipeline.run_once().status == status


def record_source(runtime, message):
    with runtime.db.connection() as connection:
        turn = connection.execute("SELECT turn_id FROM thread_messages WHERE id=?", (message,)).fetchone()[0]
    return runtime.evolution.record_experience(task_type="conversation", outcome="failure", lineage_group_hash="lineage",
        source_content_hash="source", runtime_bundle_id=runtime.behavior.active("stable").id,
        dataset_partition="DISCOVERY", idempotency_key="experience", source_kind="turn", source_id=turn)


def test_experience_memory_uses_user_provenance(tmp_path, monkeypatch):
    runtime, message = prepare(tmp_path)
    experience = record_source(runtime, message)
    stub_budget(monkeypatch, runtime)
    pipeline = build_pipeline(runtime, mode="ACTIVE", decisions=StubDecisions(runtime.db),
        agent=StubAgent(lambda ids, refs: memory_draft(experience["id"], experience_ids=tuple(ids))))
    runtime.learning.enqueue("local-user", "experience", experience["id"], "source", "root")
    job = runtime.learning.claim()
    result = pipeline.run_job(job)
    assert result.status == "APPLIED", result.reason
    with runtime.db.connection() as connection:
        refs = connection.execute("SELECT source_id FROM memory_evidence_links").fetchall()
    assert {row[0] for row in refs} == {message}
    assert user_sources(runtime.db, experience, "someone-else") == []


def test_source_change_before_promotion_is_refused(tmp_path):
    runtime, message = prepare(tmp_path)
    source = record_source(runtime, message)
    adapter = adapters(runtime)["MEMORY"]
    candidate = adapter.create_candidate(memory_draft(message), owner_id="local-user", job_id="memory-job")
    candidate["user_sources"] = user_sources(runtime.db, source, "local-user")
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE thread_messages SET content='changed' WHERE id=?", (message,))
    with pytest.raises(ValueError, match="changed or was revoked"):
        adapter.promote(candidate, owner_id="local-user")


class ReplayGateway:
    def __init__(self):
        self.calls = []
    async def complete(self, request, context=None):
        self.calls.append((request, context))
        if request.purpose == "learning_replay_select":
            task = json.loads(request.messages[-1]["content"])["task"]
            return SimpleNamespace(message=json.dumps({"load": "database" in task}))
        return SimpleNamespace(message="evidence result")


def replay_provider(tmp_path, runtime, gateway):
    path = tmp_path / "replay.json"
    suite = {"owner_id": "local-user", "target": "SKILL", "cases": [
        {"id": "relevant", "task": "diagnose database timeout", "relevant": True, "rubric": {"deterministic_required": ["evidence"]}},
        {"id": "unrelated", "task": "write a poem", "relevant": False, "rubric": {"deterministic_required": ["result"]}}]}
    path.write_text(json.dumps({"suites": [suite]}), encoding="utf-8")
    return RuntimeLearningReplay(runtime, gateway, path)


def test_runtime_replay_loads_candidate_without_activating_it(tmp_path, monkeypatch):
    runtime, _ = prepare(tmp_path)
    gateway = ReplayGateway()
    provider = replay_provider(tmp_path, runtime, gateway)
    job = skill_job(runtime, "replay")
    candidate = adapters(runtime)["SKILL"].create_candidate(skill_draft(), owner_id="local-user", job_id=job)
    candidate.update(owner_id="local-user", job_id=job, root_budget_id="root")
    monkeypatch.setattr(runtime.learning, "assert_learning_call_allowed", lambda *args: True)
    cases, runner = provider("SKILL", candidate)
    assert runner(cases[0], candidate["version_id"])["loaded_skill"] is True
    assert runner(cases[1], candidate["version_id"])["loaded_skill"] is False
    assert any(skill_draft().proposed_change["content"] == message["content"] for request, _ in gateway.calls
               if request.purpose == "learning_replay" for message in request.messages)
    assert all(context.root_budget_id == "root" and context.owner_id == "local-user" for _, context in gateway.calls)
    assert all(item["version_id"] != candidate["version_id"] for item in runtime.skill_platform.enabled_versions())
    assert provider("SKILL", {**candidate, "owner_id": "other"}) is None
    monkeypatch.setattr(runtime.learning, "assert_learning_call_allowed", lambda *args: False)
    count = len(gateway.calls)
    with pytest.raises(RuntimeError, match="expired"):
        runner(cases[0], candidate["version_id"])
    assert len(gateway.calls) == count


def test_resume_uses_saved_candidate_and_does_not_regenerate(tmp_path, monkeypatch):
    runtime, message = prepare(tmp_path)
    pipeline = skill_pipeline(runtime, monkeypatch, mode="ACTIVE")
    enqueue(runtime, message)
    first = pipeline.run_once()
    assert first.outcome == "NEEDS_REPLAY"
    from app.learning_eval import EvaluationResult
    monkeypatch.setattr(pipeline, "_evaluate", lambda *args, **kwargs: EvaluationResult(
        target="SKILL", candidate_id=first.candidate_id, rule_pass=True,
        rule_checks={"no_grant": True}, judge={"wins": 2, "losses": 0, "ties": 0},
        outcome="PASS", reason="", metrics={}))
    row = next(item for item in runtime.learning.history() if item["id"] == first.job_id)
    second = pipeline.resume(first.job_id, owner_id="local-user", expected_version=row["version"], approve=True)
    assert second.outcome == "PROMOTED", second.reason
    assert second.candidate_id == first.candidate_id
    assert pipeline.agent.calls == pipeline.decisions.calls == 1
    with pytest.raises(ValueError):
        pipeline.resume(first.job_id, owner_id="local-user", expected_version=row["version"], approve=True)


def test_unknown_cannot_be_resumed(tmp_path, monkeypatch):
    runtime, message = prepare(tmp_path)
    pipeline = skill_pipeline(runtime, monkeypatch)
    pipeline.agent.error = RuntimeError("request interrupted")
    enqueue(runtime, message)
    result = pipeline.run_once()
    assert result.status == "UNKNOWN"
    row = next(item for item in runtime.learning.history() if item["id"] == result.job_id)
    with pytest.raises(ValueError, match="not a resumable"):
        pipeline.resume(result.job_id, owner_id="local-user", expected_version=row["version"])
    assert pipeline.agent.calls == 1


def test_background_learning_advances_canaries_when_queue_empty(tmp_path, monkeypatch):
    runtime, _ = prepare(tmp_path)
    advanced = []
    runtime.learning.pipeline = SimpleNamespace(advance_canaries=lambda owner: advanced.append(owner))
    monkeypatch.setattr(runtime.learning, "collect", lambda owner: None)
    assert runtime.learning.run_once() is False
    assert advanced == ["local-user"]


@pytest.mark.parametrize("stopped,ready,expected", [(False, False, []), (False, True, ["promote"]), (True, False, ["rollback"])])
def test_canary_advancement_obeys_status(tmp_path, monkeypatch, stopped, ready, expected):
    runtime, _ = prepare(tmp_path)
    pipeline = skill_pipeline(runtime, monkeypatch, mode="ACTIVE")
    actions = []
    monkeypatch.setattr(pipeline, "_canary_candidates", lambda owner: [{"candidate_id": "candidate", "target": "BEHAVIOR"}])
    monkeypatch.setattr(pipeline.gate, "canary_status", lambda *args, **kwargs: {
        "ready": ready, "stopped": stopped, "checks": {"safety_failures": True, "success_failures": True, "permission_violation": True}})
    def action(name):
        def invoke(*args, **kwargs):
            actions.append(name)
            return SimpleNamespace(to_dict=lambda: {"outcome": name})
        return invoke
    monkeypatch.setattr(pipeline.gate, "complete_canary", action("promote"))
    monkeypatch.setattr(pipeline.gate, "rollback", action("rollback"))
    pipeline.advance_canaries()
    assert actions == expected


def test_completed_canary_is_not_scanned_again(tmp_path, monkeypatch):
    runtime, _ = prepare(tmp_path)
    pipeline = skill_pipeline(runtime, monkeypatch, mode="ACTIVE")
    for outcome in ("CANARY_STARTED", "PROMOTED"):
        pipeline.gate._record(target="BEHAVIOR", candidate_id="candidate", owner_id="local-user", outcome=outcome, checks={})
    assert pipeline._canary_candidates("local-user") == []


def test_changed_policy_blocks_target_before_generation(tmp_path, monkeypatch):
    runtime, message = prepare(tmp_path)
    policy = runtime.learning.policy()
    runtime.learning.configure("local-user", expected_version=policy["version"], paused=False, allowed_assets=["memory"])
    pipeline = skill_pipeline(runtime, monkeypatch)
    enqueue(runtime, message)
    result = pipeline.run_once()
    assert result.outcome == "TARGET_DISABLED"
    assert pipeline.agent.calls == 0


def test_resume_api_requires_csrf_and_owner_scoped_version(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.test_api import _headers
    runtime, message = prepare(tmp_path)
    runtime.learning.pipeline = skill_pipeline(runtime, monkeypatch, mode="ACTIVE")
    enqueue(runtime, message)
    result = runtime.learning.pipeline.run_once()
    row = next(item for item in runtime.learning.history() if item["id"] == result.job_id)
    app = create_app(runtime=runtime)
    client = TestClient(app)
    url = f"/api/learning/jobs/{result.job_id}/resume"
    body = {"expected_version": row["version"], "approve": False}
    assert client.post(url, json=body, headers={"host": "127.0.0.1:8000"}).status_code == 403
    assert client.post(url, json={**body, "approve": "yes"}, headers=_headers(app)).status_code == 422
    assert client.post(url, json={**body, "expected_version": -1}, headers=_headers(app)).status_code == 409
    response = client.post(url, json=body, headers=_headers(app))
    assert response.status_code == 200, response.text
    assert response.json()["candidate_id"] == result.candidate_id


def test_behavior_replay_renders_the_production_fragment(tmp_path, monkeypatch):
    from tests.test_learning_targets import behavior_draft, record_experiences
    runtime, _ = prepare(tmp_path)
    gateway = ReplayGateway()
    path = tmp_path / "behavior-replay.json"
    path.write_text(json.dumps({"suites": [{"owner_id": "local-user", "target": "BEHAVIOR", "cases": [
        {"id": "target", "task": "diagnose", "target_behavior": True, "rubric": {"deterministic_required": ["evidence"]}},
        {"id": "neighbour", "task": "summarize", "target_behavior": False, "rubric": {"deterministic_required": ["result"]}}]}]}), encoding="utf-8")
    provider = RuntimeLearningReplay(runtime, gateway, path)
    items = record_experiences(runtime)
    job = skill_job(runtime, "behavior-replay")
    candidate = adapters(runtime)["BEHAVIOR"].create_candidate(behavior_draft([item["id"] for item in items]),
        owner_id="local-user", job_id=job)
    candidate.update(owner_id="local-user", job_id=job, root_budget_id="root")
    monkeypatch.setattr(runtime.learning, "assert_learning_call_allowed", lambda *args: True)
    cases, runner = provider("BEHAVIOR", candidate)
    runner(cases[0], candidate["base_bundle_id"])
    runner(cases[0], candidate["target_bundle_id"])
    assert gateway.calls[0][0].messages != gateway.calls[1][0].messages
    assert all(request.purpose == "write_research_section" and context.root_budget_id == "root" for request, context in gateway.calls)
    assert runtime.behavior.active("stable").id == candidate["base_bundle_id"]


def test_generator_gets_runtime_patch_shape_without_holdout_data(tmp_path, monkeypatch):
    import copy
    from app.learning_agent import build_messages
    from app.real_evaluation import ResearchRoleReplayEvaluator

    runtime, _ = prepare(tmp_path)
    path = tmp_path / "replay.json"
    path.write_text(json.dumps({"suites": [{"owner_id": "local-user", "target": "BEHAVIOR", "cases": [
        {"id": "target", "task": "SECRET_HOLDOUT", "target_behavior": True,
         "rubric": {"deterministic_required": ["SECRET_GOLD"]}},
        {"id": "neighbor", "task": "OTHER_HOLDOUT", "target_behavior": False,
         "rubric": {"deterministic_required": ["OTHER_GOLD"]}}]}]}), encoding="utf-8")
    provider = RuntimeLearningReplay(runtime, ReplayGateway(), path)
    assert provider.generation_constraints("other-owner") == {}
    pipeline = skill_pipeline(runtime, monkeypatch, replay=provider)
    constraints = pipeline._constraints("local-user", {})
    patch = constraints["behavior_patch"]
    base = runtime.behavior.active("stable").manifest
    revised = copy.deepcopy(base)
    fragment = patch["change"]["researcher"]["write_research_section"]["evidence_statement"]
    patch["change"]["researcher"]["write_research_section"]["evidence_statement"] = fragment + " Separate fact from inference."
    revised.setdefault("prompts", {}).setdefault("researcher", {}).setdefault("write_research_section", {})["evidence_statement"] = patch["change"]["researcher"]["write_research_section"]["evidence_statement"]
    assert ResearchRoleReplayEvaluator.render_pair(base, revised, {"task": "test"})["single_allowed_fragment"]
    decision = LearningDecision(learn=True, target="BEHAVIOR", subtype="prompt", confidence=1,
                                importance=.8, risk="low", reason_codes=())
    messages = build_messages(decision, experience={}, constraints=constraints)
    serialized = json.dumps(messages)
    assert json.loads(messages[1]["content"])["safety_constraints"]["behavior_patch"] == patch
    assert "exact subtype, surface and nested change keys" in messages[0]["content"]
    assert "HOLDOUT" not in serialized and "GOLD" not in serialized
