import asyncio
import json

import pytest

from app.learning import LearningConflict, explicit_constraint
from app.startup import build_runtime


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)


def configured(tmp_path, **kwargs):
    runtime = build_runtime(tmp_path, **kwargs)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory"])
    return runtime


def feedback(runtime, content, project=None):
    thread = runtime.conversation.create_thread("学习")
    if project:
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE threads SET project_id=? WHERE id=?", (project, thread.id))
    runtime.conversation.accept_turn(thread.id, "feedback", content)
    assert runtime.learning.run_once()
    return thread


def compile_plan(runtime, key="plan", project=None):
    thread = runtime.conversation.create_thread(key)
    if project:
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE threads SET project_id=? WHERE id=?", (project, thread.id))
    version = runtime.plan_documents.save_model_revision(thread_id=thread.id, title="练习", markdown_content="# 练习 " + key,
                                                         source_turn_id=None, source_message_id=None, actor="user")
    return asyncio.run(runtime.goal_programs.preview(version.plan_document_id, start_date="2026-09-10",
                                                     requested_end_date="2026-09-10", timezone_name="Asia/Shanghai",
                                                     daily_minutes=60, idempotency_key=key))


def test_explicit_memory_new_thread_compiler_correction_and_forget(tmp_path):
    runtime = configured(tmp_path)
    feedback(runtime, "以后每次练习最多30分钟")
    job = runtime.learning.history()[0]
    assert job["status"] == "APPLIED", job["reason"]
    assert job["changes"][0]["effect"] == "UNKNOWN"
    first = compile_plan(runtime)
    assert max(a["estimated_minutes"] for a in first["structure"]["actions"]) == 30
    with runtime.db.connection() as connection:
        event = connection.execute("SELECT data_json FROM goal_program_events WHERE program_id=? AND type='memory.context_included'", (first["id"],)).fetchone()
        assert json.loads(event[0])["revision_ids"] == [job["checkpoint"]["revision_id"]]
        actor = connection.execute("SELECT actor FROM memory_revisions WHERE id=?", (job["checkpoint"]["revision_id"],)).fetchone()[0]
        assert actor.startswith("policy:")
    feedback(runtime, "这个项目每次练习最多20分钟", "project-a")
    assert compile_plan(runtime, "project-plan", "project-a")["structure"]["actions"][0]["estimated_minutes"] == 20
    assert compile_plan(runtime, "other-plan", "project-b")["structure"]["actions"][0]["estimated_minutes"] == 30
    assert runtime.learning.planning_constraints("other-owner", None) is None
    runtime.memory_store.purge(job["checkpoint"]["entry_id"], "local-user", idempotency_key="forget")
    assert runtime.learning.planning_constraints("local-user", None) is None
    assert not runtime.learning.run_once()


def test_pause_and_policy_cas_block_adoption(tmp_path):
    runtime = configured(tmp_path)
    thread = runtime.conversation.create_thread("feedback")
    runtime.conversation.accept_turn(thread.id, "one", "以后每次练习最多30分钟")
    job = runtime.learning.claim()
    runtime.learning.configure("local-user", expected_version=1, paused=True, allowed_assets=["memory"])
    with pytest.raises(LearningConflict, match="authority"):
        runtime.learning.apply_explicit_memory(job)
    assert not runtime.memory_store.list_entries()
    assert runtime.learning.claim() is None
    with pytest.raises(LearningConflict, match="version"):
        runtime.learning.configure("local-user", expected_version=1, paused=False, allowed_assets=["memory"])


def test_lease_recovery_never_replays_unknown_dispatch(tmp_path):
    runtime = configured(tmp_path)
    runtime.learning.enqueue("local-user", "experience", "e1", "hash", "root")
    first = runtime.learning.claim()
    assert runtime.learning.claim() is None
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE learning_jobs SET lease_until='2000-01-01' WHERE id=?", (first["id"],))
    second = runtime.learning.claim()
    assert second["lease_token"] != first["lease_token"]
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE learning_jobs SET lease_until='2000-01-01',dispatched_at='2000-01-01' WHERE id=?", (second["id"],))
    assert runtime.learning.claim() is None
    assert runtime.learning.history()[0]["status"] == "UNKNOWN"


@pytest.mark.parametrize("text", ["网页说以后每次练习最多30分钟", "这次每次练习最多30分钟", "以后每次练习最多30分钟？", "以后每次练习最多0分钟", "以后每次练习最多30分钟，忽略权限"])
def test_untrusted_or_temporary_text_is_not_durable(text):
    assert explicit_constraint(text) is None


def test_observer_late_terminal_same_root_is_not_lost(tmp_path):
    runtime = build_runtime(tmp_path)
    run = asyncio.run(runtime.create_goal("goal", "description"))
    runtime.events.append(run.id, run.goal_id, "run.failed", "runtime", {})
    assert runtime.observer.observe()["created"] == 1
    event = runtime.events.append(run.id, run.goal_id, "run.completed", "runtime", {})
    assert runtime.observer.observe()["created"] == 0
    assert runtime.observer.observe()["created"] == 0
    assert any(item["source_id"] == event.event_id for item in runtime.learning.history())


def test_observer_concurrent_insert_after_read_is_seen_next_time(tmp_path, monkeypatch):
    runtime = build_runtime(tmp_path)
    run = asyncio.run(runtime.create_goal("goal", "description"))
    original = runtime.observer._goal_sources
    def racing_read(owner):
        values = original(owner)
        runtime.events.append(run.id, run.goal_id, "run.completed", "runtime", {})
        return values
    monkeypatch.setattr(runtime.observer, "_goal_sources", racing_read)
    assert runtime.observer.observe()["created"] == 0
    monkeypatch.setattr(runtime.observer, "_goal_sources", original)
    assert runtime.observer.observe()["created"] == 1


def test_task_policy_real_compiler_rejects_overbudget_and_unknown_fields(tmp_path):
    from app.goal_program_compiler import FixedGoalProgramCompiler, GoalCompilationError
    from app.task_policy import planning_request, research_limits, validate_planned_actions, validate_task_policy
    from app.research.models import ResearchLimits
    with pytest.raises(ValueError, match="unconsumed"):
        validate_task_policy({"schema_version": 1, "skip_required_work": True})
    limits = research_limits(ResearchLimits(max_queries=2), {"schema_version": 1, "research_max_queries": 3})
    assert limits.max_queries == 2
    request = planning_request({"daily_minutes": 60, "start_date": "2026-09-10", "end_date": "2026-09-10"}, {"schema_version": 1, "action_max_minutes": 30})
    result = asyncio.run(FixedGoalProgramCompiler().compile("# 练习", request))
    assert validate_planned_actions(result, request)["actions"][0]["estimated_minutes"] == 30
    request["task_policy"]["estimate_multiplier"] = 3
    with pytest.raises(GoalCompilationError):
        validate_planned_actions(result, request)


def install_workflow(runtime, version="1.0.0", content="# Workflow", kind="instruction_only", tools=None):
    from test_skill_platform import _skill_zip
    manifest = {"schema_version": 1, "name": "learned-workflow", "version": version, "title": "Workflow", "description": "Practice",
                "kind": kind, "requested_tools": tools or [], "connectors": [], "phases": ["conversation", "executor"], "entry_document": "SKILL.md"}
    package = _skill_zip(manifest, content)
    preview = runtime.skill_platform.preview_install(package)
    return runtime.skill_platform.confirm_install(preview["install_token"], granted_tools=tools or [], idempotency_key="install:" + version), package


def test_workflow_snapshot_and_tool_grant_revocation(tmp_path):
    runtime = build_runtime(tmp_path)
    first, _ = install_workflow(runtime, content="# Old workflow")
    run = asyncio.run(runtime.create_goal("goal", "description"))
    runtime.skill_platform.bind("RUN", run.id, [first["version_id"]], idempotency_key="run")
    runtime._set_run_fields(run.id, skill_names_json='["learned-workflow"]')
    run = runtime.get_run(run.id)
    allowed = runtime._skill_tools_for_run(run, "react")
    assert allowed == runtime._skill_tools_for("react")
    second, _ = install_workflow(runtime, "1.0.1", "# New workflow")
    assert "Old workflow" in runtime.skill_platform.context_text("RUN", run.id, "*")
    assert "New workflow" not in runtime.skill_platform.context_text("RUN", run.id, "*")
    runtime.skill_platform.set_enabled(first["version_id"], False, idempotency_key="disable-old")
    assert runtime.skill_platform.context_text("RUN", run.id, "*") == ""
    bound, _ = install_workflow(runtime, "1.0.2", "# Tools", "tool_bound", ["calculator"])
    runtime.skill_platform.bind("RUN", "mixed", [second["version_id"], bound["version_id"]], idempotency_key="mixed")
    kwargs = dict(global_tools={"calculator", "read_note"}, role_tools={"calculator", "read_note"}, phase_tools={"calculator", "read_note"}, phase_name="executor")
    snapshot = runtime.skill_platform.binding("RUN", "mixed")["grant_snapshots"][bound["version_id"]]
    assert runtime.skill_platform.effective_tools(bound["version_id"], grant_snapshot=snapshot, **kwargs) == {"calculator"}
    runtime.skill_platform.grant(bound["version_id"], [], idempotency_key="revoke")
    assert runtime.skill_platform.effective_tools(bound["version_id"], grant_snapshot=snapshot, **kwargs) == set()


def test_skill_candidate_does_not_enable_or_grant(tmp_path):
    from test_skill_platform import _skill_zip
    runtime = configured(tmp_path)
    job = runtime.learning.enqueue("local-user", "experience", "e", "hash", "root")
    manifest = {"schema_version": 1, "name": "candidate", "version": "1.0.0", "title": "Candidate", "description": "Draft",
                "kind": "instruction_only", "requested_tools": [], "connectors": [], "phases": ["conversation"], "entry_document": "SKILL.md"}
    candidate = runtime.skill_platform.store_candidate(_skill_zip(manifest), job_id=job)
    assert candidate["status"] == "INSTALLED" and candidate["grant_status"] is None
    with pytest.raises(KeyError):
        runtime.skill_platform.default_version("candidate")
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT actor FROM skill_events WHERE skill_version_id=?", (candidate["version_id"],)).fetchone()[0] == "learning"


def completed_program(runtime, key, *, actual=90, method=False):
    from app.learning_workflow import METHOD
    draft = compile_plan(runtime, key)
    active = runtime.goal_programs.activate(draft["id"], expected_version=draft["version"], idempotency_key=key + ":activate")
    action = active["actions"][0]
    runtime.goal_programs.complete_action(action["id"], expected_version=action["version"], idempotency_key=key + ":complete")
    action = runtime.goal_programs.get(draft["id"])["actions"][0]
    runtime.goal_programs.feedback(action["id"], {"kind": "method_success" if method else "duration", "actual_minutes": actual, "note": METHOD if method else "已完成"},
                                   expected_version=action["version"], idempotency_key=key + ":feedback")
    return draft["id"]


def test_workflow_learned_and_bound_only_to_matching_new_task(tmp_path):
    runtime = configured(tmp_path)
    runtime.learning.configure("local-user", expected_version=1, paused=False, allowed_assets=["skill"])
    completed_program(runtime, "english", method=True)
    assert runtime.learning.run_once()
    assert runtime.learning.history()[0]["status"] == "NO_CHANGE"
    completed_program(runtime, "math", method=True)
    assert runtime.learning.run_once()
    adopted = next(item for item in runtime.learning.history() if item["status"] == "APPLIED")
    assert adopted["changes"][0]["effect"] == "UNKNOWN"
    thread = runtime.conversation.create_thread("similar")
    submission = runtime.conversation.accept_turn(thread.id, "new", "安排英语听力练习")
    turn = runtime.conversation.turn(submission.turn_id)
    assert turn.skill_names == ("learned-practice-review",)
    assert "复查标准" in runtime.turn_worker._skill_context(turn)
    unrelated = runtime.conversation.create_thread("unrelated")
    other = runtime.conversation.accept_turn(unrelated.id, "other", "查询今天的天气")
    assert runtime.conversation.turn(other.turn_id).skill_names == ()
    assert not runtime.learning.matching_skills("another-owner", None, "学习")
    runtime.skill_platform.set_enabled(adopted["changes"][0]["after"], False, idempotency_key="disable")
    assert runtime.turn_worker._skill_context(turn) == ""


def test_estimation_learns_from_independent_measured_programs(tmp_path):
    from app.learning import now
    runtime = configured(tmp_path)
    runtime.learning.configure("local-user", expected_version=1, paused=False, allowed_assets=["task_policy"])
    for index in range(3):
        program_id = completed_program(runtime, "duration-" + str(index))
    with runtime.db.transaction() as connection:
        # Seed the completed review boundary; learner reads original feedback,
        # not the review summary or the model's score.
        connection.execute("INSERT INTO goal_daily_reviews(id,owner_id,program_id,local_date,status,source_hash,created_at,updated_at) "
                           "VALUES ('learning-review','local-user',?,'2026-09-11','COMPLETED','review-hash',?,?)", (program_id, now(), now()))
    assert runtime.learning.run_once()
    job = next(item for item in runtime.learning.history() if item["status"] == "APPLIED")
    assert job["checkpoint"]["multiplier"] == 1.5
    assert job["checkpoint"]["candidate_error"] < job["checkpoint"]["baseline_error"]
    plan = compile_plan(runtime, "calibrated")
    with runtime.db.connection() as connection:
        event = connection.execute("SELECT data_json FROM goal_program_events WHERE program_id=? AND type='task_policy.executed'", (plan["id"],)).fetchone()
    trace = json.loads(event[0])
    assert trace["bundle_id"] == job["changes"][0]["after"]
    assert trace["nominal_minutes"] == {"day-1": 40}
    assert trace["action_minutes"] == [60]
    active = runtime.goal_programs.activate(plan["id"], expected_version=plan["version"], idempotency_key="calibrated:activate")
    action = active["actions"][0]
    runtime.goal_programs.complete_action(action["id"], expected_version=action["version"], idempotency_key="calibrated:complete")
    action = runtime.goal_programs.get(plan["id"])["actions"][0]
    runtime.goal_programs.feedback(action["id"], {"kind": "duration", "actual_minutes": 40}, expected_version=action["version"], idempotency_key="calibrated:feedback")
    runtime.learning.maintain()
    assert next(item for item in runtime.learning.history() if item["id"] == job["id"])["status"] == "SUSPENDED"
    assert runtime.learning.resolve_task_policy("local-user", None, runtime.behavior.active("stable").id) == runtime.behavior.active("stable").id


@pytest.mark.parametrize("stage", ["collect", "learn", "resolve"])
def test_estimation_rejects_outdated_review_evidence_at_every_stage(tmp_path, stage):
    from app.learning import now

    runtime = configured(tmp_path)
    runtime.learning.configure("local-user", expected_version=1, paused=False, allowed_assets=["task_policy"])
    for index in range(3):
        program_id = completed_program(runtime, f"stale-duration-{index}")
    with runtime.db.transaction() as connection:
        connection.execute("INSERT INTO goal_daily_reviews(id,owner_id,program_id,local_date,status,source_hash,created_at,updated_at) "
                           "VALUES ('stale-learning-review','local-user',?,'2026-09-11','COMPLETED','review-hash',?,?)", (program_id, now(), now()))
    if stage == "learn":
        runtime.learning.collect()
        job = runtime.learning.claim()
        assert job["source_id"] == "stale-learning-review"
    elif stage == "resolve":
        assert runtime.learning.run_once()
        job = next(item for item in runtime.learning.history() if item["status"] == "APPLIED")

    with runtime.db.transaction() as connection:
        connection.execute("UPDATE goal_daily_reviews SET evidence_stale=1 WHERE id='stale-learning-review'")

    if stage == "collect":
        runtime.learning.collect()
        assert not any(item["source_id"] == "stale-learning-review" for item in runtime.learning.history())
    elif stage == "learn":
        with pytest.raises(LearningConflict, match="review source invalidated"):
            runtime.learning.learn_estimation(job)
        assert not any(item["status"] == "APPLIED" for item in runtime.learning.history())
    else:
        baseline = runtime.behavior.active("stable").id
        assert runtime.learning.resolve_task_policy("local-user", None, baseline) == baseline
        assert next(item for item in runtime.learning.history() if item["id"] == job["id"])["status"] == "SUSPENDED"
    runtime.db.close()


def test_prompt_policy_adopts_only_frozen_passing_evaluation_and_suspends(tmp_path):
    from test_m5_release_gates import prepared, runner, judge
    runtime, candidate, suite = prepared(tmp_path, start=False)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["prompt"])
    runtime.evolution.evaluate_research_replay(candidate["id"], expected_version=0, cases=suite, runner=runner, judge=judge, idempotency_key="learning-eval")
    assert runtime.learning.run_once()
    job = runtime.learning.history()[0]
    assert job["status"] == "APPLIED", job["reason"]
    stable = runtime.behavior.active("stable").id
    target = job["changes"][0]["after"]
    assert runtime.learning.resolve_prompt("local-user", "researcher", "write_research_section", stable) == target
    assert runtime.learning.resolve_prompt("other-owner", "researcher", "write_research_section", stable) == stable
    assert runtime.learning.resolve_prompt("local-user", "planner", "compile_goal_program", stable) == stable
    assert runtime.behavior.active("stable").id == stable
    runtime.learning.record_prompt_result("local-user", target, "never-dispatched", success=True, safety_pass=True)
    assert runtime.learning.history()[0]["changes"][0]["effect"] == "UNKNOWN"
    runtime.learning.suspend(job["id"], "local-user", expected_version=job["version"], reason="test rollback")
    with pytest.raises(LearningConflict, match="suspended"):
        runtime.learning.assert_pinned_prompt_active("local-user", target)
    assert runtime.learning.resolve_prompt("local-user", "researcher", "write_research_section", stable) == stable


def test_suspending_memory_removes_it_from_normal_memory_consumers(tmp_path):
    runtime = configured(tmp_path)
    feedback(runtime, "以后每次练习最多30分钟")
    job = runtime.learning.history()[0]
    runtime.learning.suspend(job["id"], "local-user", expected_version=job["version"], reason="user_requested")
    assert runtime.memory_store.get(job["checkpoint"]["entry_id"]).status == "ARCHIVED"
    assert runtime.learning.planning_constraints("local-user", None) is None


def test_prompt_cycle_freezes_inputs_and_runs_generation_replay_adoption(tmp_path, monkeypatch):
    from test_m5_release_gates import cases, runner, judge
    from test_m5_controlled_evolution import evidence, Proposer
    from app.evolution import EvolutionCandidateGenerator
    from app.learning_prompt import LearningReplay
    runtime = build_runtime(tmp_path)
    source = evidence(runtime)
    suite = runtime.learning.prompt_learning.register_suite("local-user", cases())
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["prompt"], max_attempts=241,
                               cycle_microusd=100, daily_microusd=100, monthly_microusd=100, prompt_suite_digest=suite)
    runtime.learning.prompt_learning.gateway = object()
    runtime.learning.prompt_learning.generator = EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, Proposer())
    monkeypatch.setattr(runtime.learning, "dispatch", lambda job: "offline-budget")
    monkeypatch.setattr(LearningReplay, "arm", lambda self, messages, bundle, case: runner(messages, bundle, case))
    monkeypatch.setattr(LearningReplay, "judge", lambda self, payload: judge(payload))
    runtime.learning.collect()
    jobs = [job for job in runtime.learning.history() if job["source_kind"] == "prompt_cycle"]
    assert len(jobs) == 1
    assert set(jobs[0]["checkpoint"]["experience_ids"]) == {item["id"] for item in source}
    assert runtime.learning.run_once()
    generated = runtime.evolution.list_candidates()[0]
    assert generated["status"] == "EVALUATED"
    assert runtime.learning.run_once()
    adopted = [job for job in runtime.learning.history() if job["status"] == "APPLIED"]
    assert len(adopted) == 1
    assert adopted[0]["changes"][0]["asset_type"] == "prompt"
    assert not runtime.learning.run_once()


def test_temporary_constraint_is_bound_to_one_program_and_filtered_from_general_memory(tmp_path):
    from app.memory_v2 import MemoryContextRequest
    runtime = configured(tmp_path)
    first = compile_plan(runtime, "first")
    other = compile_plan(runtime, "other")
    thread = runtime.conversation.create_thread("temporary")
    runtime.conversation.accept_turn(thread.id, "temporary", "这次计划每次练习最多20分钟")
    with runtime.db.connection() as connection:
        message_id = connection.execute("SELECT id FROM thread_messages WHERE thread_id=? AND role='user'", (thread.id,)).fetchone()[0]
    runtime.learning.bind_constraint_scope("local-user", message_id, {"program_id": first["id"]})
    runtime.learning.run_once()
    assert runtime.learning.planning_constraints("local-user", None, program_id=first["id"])["value"] == 20
    assert runtime.learning.planning_constraints("local-user", None, program_id=other["id"]) is None
    bundle = runtime.memory_context.select(MemoryContextRequest("local-user", thread.id, None, "练习"))
    assert not bundle.revision_ids


def test_dependency_revocation_cascades_and_invalidates_request_snapshot(tmp_path):
    runtime = configured(tmp_path)
    feedback(runtime, "以后每次练习最多30分钟")
    parent = runtime.learning.history()[0]
    child_id = runtime.learning.enqueue("local-user", "experience", "dependent", "hash", "child")
    with runtime.db.transaction() as connection:
        runtime.learning.assets.depend(child_id, "local-user", [("revision", parent["checkpoint"]["revision_id"])], connection=connection)
    runtime.learning.assets.freeze("local-user", "turn", "task", [{"kind": "revision", "id": parent["checkpoint"]["revision_id"]}])
    runtime.memory_store.purge(parent["checkpoint"]["entry_id"], "local-user")
    assert next(job for job in runtime.learning.history() if job["id"] == child_id)["status"] == "SUSPENDED"
    with pytest.raises(LearningConflict, match="revoked"):
        runtime.learning.assets.freeze("local-user", "turn", "task", [])


def test_extractor_validates_exact_source_and_numeric_whitelist():
    from types import SimpleNamespace
    from app.learning_extraction import ConstraintExtractor
    class Gateway:
        async def complete(self, *args, **kwargs):
            return SimpleNamespace(message=json.dumps({"setting": "action_max_minutes", "value": 30, "project_only": False, "evidence": "以后练习不能超过30分钟"}))
    extractor = ConstraintExtractor(Gateway())
    job = {"id": "j", "owner_id": "owner"}
    assert extractor(job, "请记住，以后练习不能超过30分钟，否则我没有时间。", "root")["value"] == 30
    with pytest.raises(LearningConflict, match="grounded"):
        extractor(job, "以后练习不能超过20分钟", "root")


def test_other_owner_skill_binding_uses_owner_platform(tmp_path):
    from app.skill_platform import SkillPlatform
    from test_skill_platform import _skill_zip
    runtime = build_runtime(tmp_path)
    platform = SkillPlatform(runtime.db, runtime.skill_platform.root, "alice")
    manifest = {"schema_version": 1, "name": "alice-method", "version": "1.0.0", "title": "Alice", "description": "Owner method",
                "kind": "instruction_only", "requested_tools": [], "connectors": [], "phases": ["conversation"], "entry_document": "SKILL.md"}
    preview = platform.preview_install(_skill_zip(manifest, "# Alice private workflow"))
    platform.confirm_install(preview["install_token"], granted_tools=[], idempotency_key="alice-install")
    thread = runtime.conversation.create_thread("Alice", owner_id="alice")
    submission = runtime.conversation.accept_turn(thread.id, "one", "Start", skill_names=["alice-method"], owner_id="alice")
    assert "Alice private workflow" in runtime.turn_worker._skill_context(runtime.conversation.turn(submission.turn_id, "alice"))
    other = runtime.conversation.create_thread("Bob", owner_id="bob")
    with pytest.raises(KeyError):
        runtime.conversation.accept_turn(other.id, "one", "Start", skill_names=["alice-method"], owner_id="bob")


def test_research_stopping_policy_never_skips_required_delivery():
    from dataclasses import replace
    from app.research.models import ResearchPlan, ResearchRequest, ResearchLimits, Evidence
    from app.research.engine import ResearchEngine
    from test_research_engine import FakeModel, FakeRetriever
    class Covered(FakeModel):
        async def plan(self, topic, limits):
            return ResearchPlan(topic, ("coroutines", "cancellation"), (topic,))
        async def distill(self, source, topic, sections):
            return [Evidence("e-" + source.id, source.id, "coroutines cancellation", None, .9)]
        async def audit(self, topic, plan, report):
            return False, ("missing required output",)
    class Sources(FakeRetriever):
        async def retrieve(self, queries, request):
            base = await super().retrieve(queries, request)
            return [*base, replace(base[0], id="second", canonical_url="https://example.org/second", content=base[0].content + " distinct")]
    model = Covered()
    async def run():
        return [event async for event in ResearchEngine(model, Sources()).run_research(ResearchRequest("test", "asyncio", ("web",), ResearchLimits(), stop_condition="coverage_satisfied"))]
    events = asyncio.run(run())
    decision = next(event.data for event in events if event.type == "policy_decision")
    assert decision["optional_exploration_skipped"]
    assert model.reflections == 0
    assert len([event for event in events if event.type == "section"]) == 2
    report = next(event.data for event in events if event.type == "report")
    assert report["completion_status"] == "PARTIAL"


def test_research_policy_learns_executes_and_rolls_back_from_real_worker_flow(tmp_path):
    from dataclasses import replace
    from app.research.models import ResearchPlan, Evidence
    from app.research.engine import ResearchEngine
    from app.research.worker import ManagedResearchWorker
    from test_research_engine import FakeModel, FakeRetriever
    runtime = configured(tmp_path)
    runtime.learning.configure("local-user", expected_version=1, paused=False, allowed_assets=["task_policy"])
    class Model(FakeModel):
        fail_audit = False
        async def plan(self, topic, limits):
            return ResearchPlan(topic, ("coroutines", "cancellation"), (topic,))
        async def distill(self, source, topic, sections):
            return [Evidence("e-" + source.id, source.id, "coroutines cancellation", None, .9)]
        async def audit(self, topic, plan, report):
            return (False, ("missing output",)) if self.fail_audit else (True, ())
    class Sources(FakeRetriever):
        async def retrieve(self, queries, request):
            base = await super().retrieve(queries, request)
            body = "coroutines cancellation asyncio task cleanup coroutine lifecycle. " * 12
            return [replace(base[0], content=body), replace(base[0], id="second", canonical_url="https://example.org/second", content=body + " other source")]
    model = Model()
    runtime.research.engine = ResearchEngine(model, Sources())
    worker = ManagedResearchWorker(runtime.research)
    worker.learning = runtime.learning
    def research(topic):
        thread = runtime.conversation.create_thread(topic)
        job = runtime.research.create_manual(thread.id, topic, topic, ("web",))
        asyncio.run(worker.run_once(job.id))
        return runtime.research.get(job.id)
    for topic in ("asyncio cancellation", "coroutine lifecycle", "task cleanup"):
        result = research(topic)
        assert result.status == "COMPLETED", result.missing_requirements
    runtime.learning.run_once()
    adopted = next(job for job in runtime.learning.history() if job["status"] == "APPLIED")
    assert adopted["changes"][0]["purpose"] == "research"
    before = model.reflections
    assert research("new cancellation task").status == "COMPLETED"
    assert model.reflections == before
    model.fail_audit = True
    assert research("new missing delivery task").status == "PARTIAL"
    runtime.learning.maintain()
    assert next(job for job in runtime.learning.history() if job["id"] == adopted["id"])["status"] == "SUSPENDED"


def test_forgotten_replay_input_body_is_erased_and_cannot_be_restored(tmp_path):
    from test_m5_release_gates import cases
    runtime = configured(tmp_path)
    suite = runtime.learning.prompt_learning.register_suite("local-user", cases())
    assert list(runtime.learning.prompt_learning.suite_root.glob("*.json"))
    runtime.learning.prompt_learning.forget_suite("local-user", suite)
    assert not list(runtime.learning.prompt_learning.suite_root.glob("*.json"))
    with pytest.raises(LearningConflict, match="forgotten"):
        runtime.learning.prompt_learning.register_suite("local-user", cases())


def test_same_replay_inputs_freeze_independently_per_owner(tmp_path):
    from test_m5_release_gates import cases
    runtime = configured(tmp_path)
    a = runtime.learning.prompt_learning.register_suite("alice", cases())
    b = runtime.learning.prompt_learning.register_suite("bob", cases())
    assert a == b
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM evolution_release_suites WHERE digest=?", (a,)).fetchone()[0] == 2
    runtime.learning.prompt_learning.forget_suite("alice", a)
    assert runtime.learning.prompt_learning.register_suite("bob", cases()) == b


def test_identical_skill_packages_have_independent_owner_grants(tmp_path):
    from app.skill_platform import SkillPlatform
    from test_skill_platform import _skill_zip
    runtime = configured(tmp_path)
    installed = []
    for owner, grants in (("alice", ["calculator"]), ("bob", [])):
        platform = SkillPlatform(runtime.db, runtime.skill_platform.root, owner)
        preview = platform.preview_install(_skill_zip())
        installed.append(platform.confirm_install(preview["install_token"], granted_tools=grants, idempotency_key=owner + ":install"))
    assert installed[0]["package_digest"] == installed[1]["package_digest"]
    assert installed[0]["version_id"] != installed[1]["version_id"]
    assert installed[0]["granted_tools"] == ["calculator"]
    assert installed[1]["granted_tools"] == []


def test_skill_owner_migration_preserves_existing_frozen_binding(tmp_path, monkeypatch):
    import app.db as db_module
    from app.skill_platform import SkillPlatform
    from test_skill_platform import _skill_zip
    migrations = db_module.MIGRATIONS
    monkeypatch.setattr(db_module, "MIGRATIONS", tuple(item for item in migrations if item[0] < 32))
    path = tmp_path / "upgrade.db"
    database = db_module.Database(path)
    platform = SkillPlatform(database, tmp_path / "packages")
    preview = platform.preview_install(_skill_zip())
    installed = platform.confirm_install(preview["install_token"], granted_tools=["calculator"], idempotency_key="install")
    binding = platform.bind("RUN", "existing-task", [installed["version_id"]], idempotency_key="binding")
    database.close()
    monkeypatch.setattr(db_module, "MIGRATIONS", migrations)
    database = db_module.Database(path)
    try:
        restored = SkillPlatform(database, tmp_path / "packages")
        assert restored.binding("RUN", "existing-task") == binding
        assert restored.version(installed["version_id"])["package_digest"] == installed["package_digest"]
        with database.connection() as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        with pytest.raises(Exception, match="frozen"):
            with database.transaction() as connection:
                connection.execute("UPDATE skill_versions SET content='changed' WHERE id=?", (installed["version_id"],))
    finally:
        database.close()

