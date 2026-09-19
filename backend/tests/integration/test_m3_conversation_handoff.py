"""Real PostgreSQL handoff evidence; models are explicitly simulated."""
import json
import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_comparison_round_uses_deterministic_handoff_and_records_five_calls(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    from app.model_gateway import ModelProfile, ModelResponse, Timing, UsageBuckets
    from scripts import m3_live_acceptance as live

    for name in ("LLM_AP_PATH", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("M3_FAKE_KEY", "offline")
    monkeypatch.setenv("EMBEDDING_API_KEY_ENV", "M3_UNUSED_CREDENTIAL")
    monkeypatch.delenv("M3_UNUSED_CREDENTIAL", raising=False)
    seen = []

    async def transport(profile, request, **kwargs):
        seen.append(request)
        payload = {"summary": "simulated", "findings": [], "risks": [], "open_questions": []}
        valid_answer = "仅基于材料只读分析，未执行任何方案。S1显示A为130分钟，超过120分钟；S2显示B为100分钟但需批准且状态未知；方案C在S3与S4中存在执行50与90分钟的矛盾，总时长可能为90或130分钟。"
        if request.purpose == "expert_researcher":
            payload = {"summary": "S3与S4分别为50和90，C总时长可能90或130，资料差异待核实。", "findings": [{"text": "gap", "confidence": .9, "source_refs": ["S3", "S4"]}], "risks": [], "open_questions": []}
        elif request.purpose == "expert_planner":
            payload = {"summary": "S1显示A为130超过120；S2显示B为100但批准依赖未知。", "findings": [{"text": "dependency", "confidence": .9, "source_refs": ["S1", "S2"]}], "risks": [], "open_questions": []}
        elif request.purpose == "expert_critic":
            payload = {"summary": "S3与S4矛盾，存在错误执行风险。", "findings": [{"text": "conflict", "confidence": .9, "source_refs": ["S3", "S4"]}], "risks": ["风险"], "open_questions": []}
        elif request.purpose in {"synthesize_experts", "single_agent_baseline"}:
            payload = valid_answer
        if request.purpose == "route_and_respond":
            payload = {"v": 4, "policy": "start_expert", "content_shape": "expert",
                       "reason_code": "explicit_collaboration", "expert": {
                           "objective": live.CORE_OBJECTIVE, "roles": ["researcher", "planner", "critic"]}}
        message = (json.dumps(payload) if isinstance(payload, dict) else payload) + "\n"
        if kwargs.get("on_text_delta"):
            kwargs["on_text_delta"](message)
        now = time.perf_counter()
        return ModelResponse(message, [], "stop", UsageBuckets(10, 0, 0, 5, 0), Timing(now, now, now), 1)

    monkeypatch.setattr(live, "runtime_attempt", transport)
    profile = ModelProfile("https://example.invalid/v1", "deepseek-v4-flash", "M3_FAKE_KEY",
                           max_attempts=1, network_retries=0, max_output_tokens=8192, provider_name="deepseek")
    budget = live.BatchBudget()
    report = await live._run_round(tmp_path, migrated_postgres_url, profile, 1, budget)
    assert len(seen) == len(report["attempts"]) == 5
    baseline = next(item for item in seen if item.purpose == "single_agent_baseline")
    baseline_context = json.loads(baseline.messages[1]["content"])["context"]
    experts = [item for item in seen if item.purpose.startswith("expert_")]
    assert len(experts) == 3
    assert all(json.loads(item.messages[1]["content"])["context"] == baseline_context for item in experts)
    assert report["comparison"]["quality_review_status"] == "PENDING"
    assert report["comparison"]["single_agent"]
    assert report["charged_microusd"] <= 7 * live.WORST_ATTEMPT_MICROUSD
    assert report["source_turn_id"] == baseline_context["source_turn_id"]
    assert len(budget.network_attempts) == 5
    assert all(len(item["prompt_digest"]) == len(item["output_digest"]) == 64 for item in budget.network_attempts)
    assert all(item["output_tokens"] == 5 and item["usage_status"] == "COMPLETE" for item in report["attempts"])
    assert report["comparison"]["rubric_digest"] == live._digest(live.QUALITY_RUBRIC)
    assert sum(report["comparison"]["cost_microusd"].values()) == report["charged_microusd"]
    assert all("m3-comparison-rubric-v1" not in json.dumps(item.messages) for item in seen)


@pytest.mark.asyncio
async def test_experts_inherit_persisted_conversation_and_bundle(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    from app.agents import ManagedAgentWorker
    from app.startup import build_runtime

    for name in ("LLM_AP_PATH", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBEDDING_API_KEY_ENV", "M3_UNUSED_CREDENTIAL")
    monkeypatch.delenv("M3_UNUSED_CREDENTIAL", raising=False)

    class RouteModel:
        async def route_and_respond(self, *, content, on_text_delta, **kwargs):
            if content == "协作检查，预算仍为两小时":
                header = {"v": 4, "policy": "start_expert", "content_shape": "expert",
                          "reason_code": "explicit_collaboration",
                          "expert": {"objective": "检查方案", "roles": ["researcher", "planner", "critic"]}}
                on_text_delta(json.dumps(header) + "\n")
            else:
                on_text_delta('{"v":1,"policy":"answer","content_shape":"text","reason_code":"content_only"}\n已记录。')

    seen = []

    class ExpertModel:
        async def execute_bundle(self, role, objective, context, inputs, manifest):
            seen.append((role, context, manifest))
            return {"summary": "模拟专家结果", "findings": [], "risks": [], "open_questions": []}

    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url, conversation_model=RouteModel())
    try:
        thread = runtime.conversation.create_thread("M3 isolated handoff")
        foreign = runtime.conversation.create_thread("unrelated thread")
        for target, key, content in (
            (foreign, "foreign", "FOREIGN_CONTEXT_MUST_NOT_LEAK"),
            (thread, "source", "来源 S1：只允许两小时，不允许外部搜索。"),
        ):
            accepted = runtime.conversation.accept_turn(target.id, key, content, [])
            await runtime.turn_worker.run_once()
            assert runtime.conversation.turn(accepted.turn_id).status == "COMPLETED"
        plan = runtime.plan_documents.save_model_revision(
            thread_id=thread.id, title="Frozen plan", markdown_content="# Plan\n\nPLAN_V1_ONLY: two hours.",
            source_turn_id=None, source_message_id=None, actor="model",
        )
        accepted = runtime.conversation.accept_turn(thread.id, "expert", "协作检查，预算仍为两小时", [])
        pinned = runtime.conversation.turn(accepted.turn_id).runtime_bundle_id
        changed = runtime.behavior.ensure({"prompt": "changed-after-acceptance"})
        runtime.behavior.activate("stable", changed.id, "switch-before-handoff")
        await runtime.turn_worker.run_once()
        run = runtime.agent_tasks.latest_run_for_thread(thread.id)
        assert run["runtime_bundle_id"] == pinned != changed.id
        snapshot = runtime.agent_tasks.context(run["context_snapshot_id"])
        assert snapshot["source_turn_id"] == accepted.turn_id
        assert snapshot["request"] == "协作检查，预算仍为两小时"
        serialized = json.dumps(snapshot, ensure_ascii=False)
        assert "来源 S1" in serialized
        assert "FOREIGN_CONTEXT_MUST_NOT_LEAK" not in serialized
        assert snapshot["plan_source"]["version_id"] == plan.id
        assert snapshot["plan_source"]["content_hash"] == plan.content_hash
        assert "PLAN_V1_ONLY" in serialized
        runtime.plan_documents.save_model_revision(
            thread_id=thread.id, title="Updated plan", markdown_content="# Plan\n\nPLAN_V2_ONLY: changed.",
            source_turn_id=None, source_message_id=None, actor="model",
            expected_version_id=plan.id, expected_file_hash=plan.content_hash,
        )
        assert runtime.agent_tasks.context(run["context_snapshot_id"]) == snapshot
        assert "PLAN_V2_ONLY" not in serialized
        worker = ManagedAgentWorker(runtime.agent_tasks, ExpertModel())
        for _ in range(5):
            await worker.run_once()
        assert runtime.agent_tasks.get_run(run["id"])["status"] == "SUCCEEDED"
        assert {role for role, _, _ in seen} == {"researcher", "planner", "critic"}
        assert all(context == snapshot for _, context, _ in seen)
        assert all(manifest != changed.manifest for _, _, manifest in seen)
        with runtime.db.connection() as connection:
            row = connection.execute(
                "SELECT runtime_bundle_id,content_json FROM agent_context_snapshots WHERE id=?",
                (run["context_snapshot_id"],),
            ).fetchone()
            assert row["runtime_bundle_id"] == pinned
            assert json.loads(row["content_json"]) == snapshot
    finally:
        runtime.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "takeover"])
async def test_postgres_fences_old_coordinator_after_service_restart(
    migrated_postgres_url, tmp_path, operation,
):
    from app.agents import AgentTaskService, ManagedAgentWorker
    from app.behavior import BehaviorBundleService
    from app.db import Database

    started, stopped = asyncio.Event(), asyncio.Event()

    class Model:
        async def execute(self, *args):
            return {"summary": "persisted expert", "findings": [], "risks": [], "open_questions": []}

        async def synthesize(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    db = Database(migrated_postgres_url, workspace=tmp_path / "worker")
    other_db = None
    pending = None
    try:
        bundle = BehaviorBundleService(db).ensure({"prompt": "frozen"})
        service = AgentTaskService(db)
        run = service.create_run("local-user", "review", {"source": "v1"}, bundle.id,
                                 expert_roles=("critic",), idempotency_key="pg-fencing")
        worker = ManagedAgentWorker(service, Model(), lease_seconds=30)
        await worker.run_once()
        await worker.run_once()
        expert = service.children(run["coordinator_task_id"])[0]
        pending = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), 3)
        old = service.get_task(run["coordinator_task_id"])

        # A fresh connection and service recover solely from PostgreSQL state.
        other_db = Database(migrated_postgres_url, workspace=tmp_path / "recovered")
        recovered = AgentTaskService(other_db)
        assert recovered.context(run["context_snapshot_id"])["source"] == "v1"
        assert recovered.get_task(expert["id"])["result_artifact_id"] == expert["result_artifact_id"]
        if operation == "cancel":
            recovered.cancel_run(run["id"], "cancel from recovered service")
        else:
            with other_db.transaction() as connection:
                connection.execute("UPDATE agent_tasks SET lease_until=? WHERE id=?",
                                   ("2000-01-01T00:00:00+00:00", old["id"]))
            replacement = recovered.claim_next("replacement", 30)
            assert replacement["id"] == old["id"]
            assert replacement["lease_epoch"] > old["lease_epoch"]
            recovered.complete(replacement["id"], "replacement", replacement["lease_epoch"],
                               "expert_synthesis", {"summary": "replacement result"})
        await asyncio.wait_for(stopped.wait(), 3)
        await asyncio.wait_for(pending, 3)
        with pytest.raises(PermissionError):
            service.complete(old["id"], worker.owner, old["lease_epoch"],
                             "expert_synthesis", {"summary": "stale result"})
        terminal = recovered.get_task(old["id"])
        if operation == "cancel":
            assert recovered.get_run(run["id"])["status"] == "CANCELLED"
            assert terminal["result_artifact_id"] is None
        else:
            assert recovered.artifact(terminal["result_artifact_id"])["content"]["summary"] == "replacement result"
        assert recovered.get_task(expert["id"])["result_artifact_id"] == expert["result_artifact_id"]
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if other_db is not None:
            other_db.close()
        db.close()
