"""A newly integrated business adapter sees its committed snapshot in PostgreSQL."""
from __future__ import annotations

import json

import pytest

from app.db import Database
from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
from app.model_admin import ModelAdminService
from app.research.live import LiveResearchModel
from app.research.models import ResearchLimits
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_snapshot_gateway import _answer, _configured_control_plane


@pytest.mark.parametrize("boundary", ["before_gateway", "retry", "judge"])
def test_rp14_pg_source_dependency_is_checked_at_each_send(migrated_postgres_url, tmp_path, monkeypatch, boundary):
    from test_research_snapshot_replay import assert_source_revocation_blocks_send
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "enforce")
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        assert_source_revocation_blocks_send(tmp_path, monkeypatch, boundary, database=db)
    finally:
        db.close()


def test_rp12_rp15_rp18_research_pair_pg_owner_root_and_cost(migrated_postgres_url, tmp_path, monkeypatch):
    """Real Research writes + dual Judge on a guarded PG ledger; no network."""
    from test_research_snapshot_replay import paired_setup
    from app.real_evaluation import ResearchRoleReplayEvaluator
    from app.model_input_snapshot_store import ModelInputSnapshotStore, ModelInvocationMissing
    from app.research_replay import authoritative_cost
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "enforce")
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        db, base, candidate, runner, config, observers, case, stable = paired_setup(tmp_path, monkeypatch, database=db, controlled=True)
        report = ResearchRoleReplayEvaluator().evaluate(base_manifest=base.manifest, candidate_manifest=candidate.manifest,
            baseline_bundle_id=base.id, candidate_bundle_id=candidate.id, cases=[case], runner=runner.runner(config), judge=runner.judge(config))
        assert report["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert report["cost_microusd"] == 8 and report["checks"]["cost_known"]
        assert len(runner.bindings) == 4 and all(row["root_budget_id"] == config["root_budget_id"] for row in runner.bindings)
        for row in runner.bindings:
            with pytest.raises(ModelInvocationMissing):
                ModelInputSnapshotStore(db).load_for_invocation("another-owner", row["id"])
            assert row["attempt_prices"][0]["price_snapshot_id"]
        with db.connection() as connection:
            root = connection.execute("SELECT * FROM task_budget_roots WHERE id=?", (config["root_budget_id"],)).fetchone()
            assert root["owner_id"] == "operator" and root["attempts_started"] == 4
            assert connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()[0] == stable.id
            costs = [row[0] for row in connection.execute("SELECT cost_microusd FROM model_attempts").fetchall()]
        assert sum(costs) == report["cost_microusd"]
        trace = [{"invocation_id": row["id"]} for row in runner.bindings]
        assert authoritative_cost(db, trace, mode="controlled")["cost_microusd"] == 8
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["snapshot", "binding"])
async def test_rp18_research_pg_atomic_failure_is_zero_send(migrated_postgres_url, tmp_path, monkeypatch, failure):
    from app.research_replay import offline_plane, policy, DEFAULT_EVIDENCE_STATEMENT, ScriptedProvider
    from app.research.models import Evidence
    import asyncio
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "observe")
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        control, bundles, routing, _ = offline_plane(db, "pg-replay-owner")
        bundle = bundles.ensure({**routing, **policy(DEFAULT_EVIDENCE_STATEMENT)})
        root = control.costs.create_default_root_budget("pg-replay-owner", "evaluation", "atomic-replay")
        observer = ScriptedProvider(db, "pg-replay-owner", [], asyncio.Event())
        gateway = RoutedModelGateway(db, control, execute_attempt=observer)
        model = LiveResearchModel(gateway)
        from app.execution_context import create_root_context
        context = ModelCallContext.from_harness(create_root_context(owner_id="pg-replay-owner", runtime_bundle_id=bundle.id, root_budget_id=root["id"]),
                                              role="researcher", purpose="research_replay")
        table = "model_input_snapshots" if failure == "snapshot" else "model_invocations"
        with db.connection() as connection:
            connection.execute("CREATE FUNCTION reject_replay_binding() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'replay atomic fault'; END; $$ LANGUAGE plpgsql")
            connection.execute(f"CREATE TRIGGER reject_replay BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION reject_replay_binding()")
        token = gateway.set_call_context(context)
        try:
            with pytest.raises(Exception, match="replay atomic fault"):
                await model.write("Setup", "Measured", [Evidence("e1", "s1", "Evidence.", None, 1)], "")
        finally:
            gateway.reset_call_context(token)
            with db.connection() as connection:
                connection.execute(f"DROP TRIGGER reject_replay ON {table}")
                connection.execute("DROP FUNCTION reject_replay_binding()")
        assert observer.trace == [] and gateway.current_call_context() is None
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
    finally:
        db.close()


def test_rp14_pg_exhausted_evaluation_budget_blocks_new_research_send(migrated_postgres_url, tmp_path, monkeypatch):
    from test_research_snapshot_replay import paired_setup
    from app.model_gateway import GatewayError
    from app.research_replay import validate_pair
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "enforce")
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        db, base, candidate, runner, config, observers, case, _ = paired_setup(tmp_path, monkeypatch, database=db, controlled=True)
        runner.control_store.costs.set_budget("operator", "ROOT", config["root_budget_id"], 0)
        with pytest.raises(GatewayError, match="budget"):
            runner.runner(config)(validate_pair(base.manifest, candidate.manifest, case)["base_messages"], base.id, case)
        assert observers == []
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
    finally:
        db.close()


def test_t12_t13_skill_pipeline_generation_selector_replay_and_judge(migrated_postgres_url, tmp_path, monkeypatch):
    from pathlib import Path
    from app.startup import build_runtime
    from app.learning_agent import LearningAgent
    from app.learning_eval import LearningJudge
    from app.learning_replay import RuntimeLearningReplay
    from app.learning_pipeline import build_pipeline
    from app.learning_decision import LearningDecision
    from app.learning import digest
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from test_learning_pipeline_v3 import StubDecisions
    from test_learning_targets import skill_draft

    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "observe")
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "OFF")
    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url)
    try:
        db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, database=runtime.db)
        gateway = RoutedModelGateway(db, runtime.model_control_store)
        replay = RuntimeLearningReplay(runtime, gateway, Path(__file__).parents[1] / "fixtures/snapshot-skill-replay.json")
        runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["skill"],
            cycle_microusd=100000, daily_microusd=100000, monthly_microusd=100000, max_attempts=20)
        content = "请固定数据库超时的排查步骤，先检查连接池，再检查慢 SQL。"
        thread = runtime.conversation.create_thread("Skill learning")
        runtime.conversation.accept_turn(thread.id, "skill", content)
        with db.connection() as connection:
            message = connection.execute("SELECT id FROM thread_messages WHERE thread_id=? AND role='user'", (thread.id,)).fetchone()[0]
        job_id = runtime.learning.enqueue("local-user", "thread_message", message, digest(content), message)
        draft = skill_draft(evidence_refs=(message,), experience_ids=(), source_refs=(message,)).to_dict()
        observer = CommittedSnapshotTransport(db, "local-user", "CS-LN-01", None)

        async def execute(profile, request, **kwargs):
            if request.purpose == "generate_learning_candidate":
                body = json.dumps(draft)
            elif request.purpose == "learning_replay_select":
                body = json.dumps({"load": "timeout" in request.messages[-1]["content"]})
            elif request.purpose == "judge_learning_candidate":
                payload = json.loads(request.messages[-1]["content"])
                body = json.dumps({"winner":"left" if "improved" in payload["left"] else "right",
                    "correctness":1,"helpfulness":1,"safety":1,"efficiency":1,"reason_codes":["supported"]})
            else:
                body = "supported improved" if len(request.messages) == 3 else "supported baseline"
            observer.callsite_id = "CS-EV-02" if request.purpose.startswith("learning_replay") else "CS-LN-01"
            observer.response = _answer(body)
            return await observer(profile, request, **kwargs)

        gateway._execute_attempt = execute
        decision = LearningDecision(learn=True, target="SKILL", subtype="", confidence=1, importance=.9,
            risk="low", reason_codes=("repeatable_workflow",))
        runtime.learning.pipeline = build_pipeline(runtime, mode="SHADOW", decisions=StubDecisions(db, decision),
            agent=LearningAgent(gateway), judge=LearningJudge(gateway), replay=replay)
        assert runtime.learning.run_once()
        job = next(item for item in runtime.learning.history() if item["id"] == job_id)
        assert job["status"] == "NO_CHANGE", job["reason"]
        assert observer.send_count == 7
        assert len({item["snapshot_id"] for item in observer.observations}) == 7
        with db.connection() as connection:
            rows = connection.execute("SELECT * FROM model_invocations ORDER BY created_at,id").fetchall()
        assert all(row["owner_id"] == job["owner_id"] and row["root_budget_id"] == job["root_budget_id"] and row["runtime_bundle_id"] == bundle.id for row in rows)
        snapshots = [ModelInputSnapshotStore(db).load(job["owner_id"], row["context_snapshot_id"]) for row in rows]
        judge = next(item for item in snapshots if item.purpose == "judge_learning_candidate")
        assert "fixed rubric" in judge.to_request().messages[0]["content"]
        assert "supported improved" in judge.content_json and "supported baseline" in judge.content_json
        assert "数据库超时诊断" not in judge.content_json
        import hashlib
        rubric = next(source for source in judge.provenance().sources if source.id == "learning-judge-rubric")
        assert rubric.content_digest == hashlib.sha256(judge.to_request().messages[0]["content"].encode()).hexdigest()
        assert rubric.location["message_index"] == 0
        assert gateway.current_call_context() is None
    finally:
        runtime.db.close()


@pytest.mark.parametrize("entry", ["generator", "extractor"])
def test_t11_learning_entries_inherit_authorized_job_and_root(migrated_postgres_url, tmp_path, monkeypatch, entry):
    from app.startup import build_runtime
    from app.learning_agent import LearningAgent
    from app.learning_extraction import ConstraintExtractor
    from app.learning_pipeline import build_pipeline
    from app.learning import digest
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from test_learning_pipeline_v3 import StubDecisions
    from test_learning_targets import memory_draft

    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "observe")
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "OFF")
    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url)
    try:
        db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, database=runtime.db)
        runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory"],
            cycle_microusd=100000, daily_microusd=100000, monthly_microusd=100000)
        content = "以后生产数据库不能由 Agent 自动重启，必须人工批准。" if entry == "generator" else "请记住，日常练习的时长上限是30分钟。"
        thread = runtime.conversation.create_thread("Learning")
        runtime.conversation.accept_turn(thread.id, "learning", content)
        with db.connection() as connection:
            message = connection.execute("SELECT id FROM thread_messages WHERE thread_id=? AND role='user'", (thread.id,)).fetchone()[0]
        job_id = runtime.learning.enqueue("local-user", "thread_message", message, digest(content), message)
        draft = memory_draft(message).to_dict()
        draft["target"] = "MEMORY"
        draft["experience_ids"] = []
        reply = json.dumps(draft) if entry == "generator" else json.dumps({
            "setting":"action_max_minutes", "value":30, "project_only":False, "evidence":"30分钟"})
        observer = CommittedSnapshotTransport(db, "local-user", "CS-LN-01", _answer(reply))
        gateway = RoutedModelGateway(db, runtime.model_control_store, execute_attempt=observer)
        if entry == "generator":
            runtime.learning.pipeline = build_pipeline(runtime, mode="SHADOW", decisions=StubDecisions(db), agent=LearningAgent(gateway))
        else:
            runtime.learning.constraint_extractor = ConstraintExtractor(gateway)
        assert runtime.learning.run_once()
        job = next(item for item in runtime.learning.history() if item["id"] == job_id)
        assert job["status"] == ("NO_CHANGE" if entry == "generator" else "APPLIED"), job["reason"]
        assert observer.send_count == 1 and gateway.current_call_context() is None
        with db.connection() as connection:
            row = connection.execute("SELECT i.*,r.owner_id AS root_owner,r.root_object_id FROM model_invocations i JOIN task_budget_roots r ON r.id=i.root_budget_id").fetchone()
        assert row["owner_id"] == row["root_owner"] == job["owner_id"]
        assert row["root_object_id"] == job_id and row["root_budget_id"] == job["root_budget_id"]
        assert row["runtime_bundle_id"] == bundle.id
        frozen = ModelInputSnapshotStore(db).load(job["owner_id"], row["context_snapshot_id"])
        assert content in json.dumps(frozen.to_request().messages, ensure_ascii=False).replace('\\"', '"')
        source = next(source for source in frozen.provenance().sources if source.id == message)
        assert source.included and source.location["message_index"] == 1
        if entry == "extractor":
            import hashlib
            assert source.content_digest == hashlib.sha256(content.encode()).hexdigest()
        else:
            assert source.location["scope"] == "available_evidence_reference"
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_t10_interleaved_expert_owners_keep_separate_postgres_budgets(migrated_postgres_url, tmp_path, monkeypatch):
    from test_snapshot_agent_entries import exercise_interleaved_experts

    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "observe")
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        await exercise_interleaved_experts(tmp_path, monkeypatch, False, database=db)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_t35_program_owner_and_operation_roots_survive_repair(migrated_postgres_url, tmp_path, monkeypatch):
    from app.conversation import ConversationService
    from app.goal_program_compiler import GoalProgramCompiler, FixedGoalProgramCompiler
    from app.goal_programs import GoalProgramService
    from app.goal_adjustments import GoalAdjustmentService
    from app.goal_reviews import GoalReviewService, ManagedGoalReviewWorker
    from test_goal_program_compiler import fixture

    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "observe")
    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    monkeypatch.setattr("app.goal_adjustments._local_date", lambda _: "2026-09-01")
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, database=db, owner_id="tenant-b")
        conversation = ConversationService(db)
        thread = conversation.create_thread("Program", owner_id="tenant-b")
        version = conversation.plan_documents.save_model_revision(thread_id=thread.id, title="Plan", markdown_content="# Plan",
                    source_turn_id=None, source_message_id=None, actor="user")
        goals = GoalProgramService(db, FixedGoalProgramCompiler(fixture()), plan_documents=conversation.plan_documents, conversation=conversation)
        draft = await goals.preview(version.plan_document_id, start_date="2026-09-01", timezone_name="Asia/Shanghai",
                                    daily_minutes=60, requested_end_date="2026-09-07", idempotency_key="preview", owner_id="tenant-b")
        active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate", owner_id="tenant-b")
        observer = CommittedSnapshotTransport(db, "tenant-b", "CS-GP-01", [
            _answer("bad JSON"), _answer('{"summary":"Done","encouragement":"Continue","needs_adjustment":false,"adjustment_reason":""}'),
            _answer('{"summary":"Period complete"}'),
        ])
        gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
        compiler = GoalProgramCompiler(gateway)
        goals.compiler = compiler
        adjustments = GoalAdjustmentService(goals, compiler, conversation.plan_documents)
        reviews = GoalReviewService(goals, compiler, adjustments, queue_delay_seconds=0)
        goals.reviews = reviews
        goals.complete_action(active["actions"][0]["id"], expected_version=0, idempotency_key="done", owner_id="tenant-b")
        goals.close_day(active["id"], "2026-09-01", idempotency_key="close", owner_id="tenant-b")
        assert await ManagedGoalReviewWorker(reviews).run_once()
        assert reviews.for_program_date(active["id"], "2026-09-01", "tenant-b")["status"] == "COMPLETED"
        adjusted = fixture()
        adjusted["actions"][1]["title"] = "Adjusted task"
        observer.callsite_id = "CS-GP-02"
        observer.response = [_answer("bad JSON"), _answer(json.dumps(adjusted))]
        current = goals.get(active["id"], "tenant-b")
        proposal = await adjustments.propose(active["id"], reason="Adjust", expected_version=current["version"],
            idempotency_key="adjust", owner_id="tenant-b")
        assert proposal["status"] == "PENDING"
        observer.callsite_id = "CS-GP-01"
        observer.response = _answer('{"summary":"Period complete"}')
        for action in active["actions"][1:]:
            goals.complete_action(action["id"], expected_version=0, idempotency_key=action["id"], owner_id="tenant-b")
        current = goals.get(active["id"], "tenant-b")
        goals.transition(active["id"], "complete", expected_version=current["version"], idempotency_key="finish", owner_id="tenant-b")
        assert await goals.period_summary(active["id"], "tenant-b") == "Period complete"
        assert gateway.current_call_context() is None
        with db.connection() as connection:
            rows = connection.execute("SELECT i.*,r.owner_id AS budget_owner,r.root_kind FROM model_invocations i JOIN task_budget_roots r ON r.id=i.root_budget_id ORDER BY i.created_at,i.id").fetchall()
        assert len(rows) == 5 and len(observer.observations) == 5
        assert all(row["owner_id"] == row["budget_owner"] == "tenant-b" and row["root_kind"] == "goal_operation" for row in rows)
        assert rows[0]["root_budget_id"] == rows[1]["root_budget_id"] != rows[2]["root_budget_id"]
        assert rows[2]["root_budget_id"] == rows[3]["root_budget_id"] != rows[4]["root_budget_id"]
        assert len({row["context_snapshot_id"] for row in rows}) == 5
    finally:
        db.close()


@pytest.mark.asyncio
async def test_research_plan_is_committed_before_postgres_transport_send(migrated_postgres_url, tmp_path, monkeypatch):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        db, bundle, _ = _configured_control_plane(
            tmp_path, monkeypatch, database=db, owner_id="pg-research-owner",
        )
        response = _answer(json.dumps({"title": "PG", "sections": ["结果", "依据"], "queries": ["PG 结果"]}, ensure_ascii=False))
        observer = CommittedSnapshotTransport(db, "pg-research-owner", "CS-RS-01", response)
        gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
        context = ModelCallContext(role="researcher", purpose="research_structured_step",
                                   owner_id="pg-research-owner", runtime_bundle_id=bundle.id)
        token = gateway.set_call_context(context)
        try:
            plan = await LiveResearchModel(gateway).plan("PG", ResearchLimits())
        finally:
            gateway.reset_call_context(token)
        assert plan.title == "PG"
        assert len(observer.observations) == 1
        assert observer.observations[0]["snapshot_id"]
        assert observer.observations[0]["attempt_id"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_model_admin_verification_failure_is_bound_and_owner_scoped_in_postgres(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    import httpx
    from app.model_gateway import GatewayError

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        observations = []
        recorder = CommittedSnapshotTransport(db, "pg-admin-owner", "CS-MD-01", None)

        def fail_after_commit(request: httpx.Request) -> httpx.Response:
            with db.connection() as connection:
                row = connection.execute(
                    "SELECT a.id AS attempt_id,i.id AS invocation_id,i.owner_id,i.purpose,"
                    "i.context_snapshot_id,i.context_snapshot_digest,i.status AS invocation_status "
                    "FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id "
                    "WHERE a.status='STARTED' ORDER BY a.started_at DESC,a.id DESC LIMIT 1",
                ).fetchone()
            assert row is not None
            assert row["owner_id"] == "pg-admin-owner"
            assert row["purpose"] == "verify_model_profile_version"
            assert row["context_snapshot_id"] and row["context_snapshot_digest"]
            assert row["invocation_status"] == "RUNNING"
            assert request.url.path.endswith("/chat/completions")
            from app.model_input_snapshot_store import ModelInputSnapshotStore
            from test_snapshot_gateway import _registered_profile
            with db.connection() as connection:
                profile_id = connection.execute("SELECT profile_version_id FROM model_attempts WHERE id=?", (row["attempt_id"],)).fetchone()[0]
            frozen = ModelInputSnapshotStore(db).load("pg-admin-owner", row["context_snapshot_id"])
            assert json.loads(request.content)["messages"] == frozen.to_request().messages
            recorder.record(_registered_profile(db, profile_id), frozen.to_request())
            observations.append(dict(row))
            raise httpx.ConnectError("provider unavailable", request=request)

        service = ModelAdminService(
            db, owner_id="pg-admin-owner", control_store=ModelControlStore(db),
            verification_transport=httpx.MockTransport(fail_after_commit),
        )
        created = service.create_profile({
            "name": "PG verification", "provider_protocol": "openai_compatible",
            "provider_name": "test", "base_url": "https://provider.test/v1",
            "model_name": "verify-v1", "credential_env_ref": "PG_ADMIN_TEST_KEY",
            "capabilities": {"text": True}, "context_window": 32768,
            "max_output_tokens": 1024, "timeout_seconds": 10, "max_attempts": 2,
        }, validate_capacity=False)
        version_id = created["versions"][0]["id"]
        monkeypatch.setenv("PG_ADMIN_TEST_KEY", "test-secret")

        other = ModelAdminService(db, owner_id="other-pg-owner", control_store=ModelControlStore(db))
        with pytest.raises(KeyError):
            await other.verify(version_id)
        assert observations == []

        with pytest.raises(GatewayError, match="retry budget exhausted"):
            await service.verify(version_id)

        assert len(observations) == 2
        assert len({item["invocation_id"] for item in observations}) == 1
        assert len({item["context_snapshot_id"] for item in observations}) == 1
        assert len({item["attempt_id"] for item in observations}) == 2
        assert service.version(version_id)["verification_status"] == "FAILED"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_model_admin_snapshot_persistence_failure_prevents_postgres_send(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    import httpx
    from app.db import Database
    from app.model_admin import ModelAdminService
    from app.model_control import ModelControlStore

    db = Database(migrated_postgres_url, workspace=tmp_path)
    sends = []

    def unexpected_send(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    try:
        service = ModelAdminService(
            db, owner_id="pg-storage-owner", control_store=ModelControlStore(db),
            verification_transport=httpx.MockTransport(unexpected_send),
        )
        created = service.create_profile({
            "name": "PG persistence failure", "provider_protocol": "openai_compatible",
            "provider_name": "test", "base_url": "https://provider.test/v1",
            "model_name": "verify-v1", "credential_env_ref": "PG_ADMIN_STORAGE_KEY",
            "capabilities": {"text": True}, "context_window": 32768,
            "max_output_tokens": 1024, "timeout_seconds": 10, "max_attempts": 1,
        }, validate_capacity=False)
        version_id = created["versions"][0]["id"]
        monkeypatch.setenv("PG_ADMIN_STORAGE_KEY", "test-secret")
        with db.connection() as connection:
            connection.execute("""
                CREATE FUNCTION reject_snapshot_insert() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'injected snapshot persistence failure'; END;
                $$ LANGUAGE plpgsql
            """)
            connection.execute("""
                CREATE TRIGGER reject_snapshot_insert_before_send
                BEFORE INSERT ON model_input_snapshots
                FOR EACH ROW EXECUTE FUNCTION reject_snapshot_insert()
            """)

        with pytest.raises(Exception, match="injected snapshot persistence failure"):
            await service.verify(version_id)

        assert sends == []
        assert service.version(version_id)["verification_status"] == "FAILED"
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
            connection.execute("DROP TRIGGER reject_snapshot_insert_before_send ON model_input_snapshots")
            connection.execute("DROP FUNCTION reject_snapshot_insert()")
    finally:
        db.close()
