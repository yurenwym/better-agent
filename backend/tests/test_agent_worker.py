import asyncio
import pytest

from app.agents import AgentTaskService, ManagedAgentWorker
from app.behavior import BehaviorBundleService
from app.db import Database


class ExpertModel:
    async def execute(self, role, objective, context, inputs):
        return {
            "summary": f"{role}:{objective}",
            "findings": [{"text": f"finding-{role}", "confidence": .8, "source_refs": []}],
            "risks": [],
            "open_questions": [],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["synthesis", "safety"])
async def test_cancellation_stops_pending_synthesis_or_safety_judge(tmp_path, stage):
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Model(ExpertModel):
        async def judge(self, result):
            return await self.synthesize(None, None, None)

        async def synthesize(self, objective, experts, failed_roles):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    db = Database(tmp_path / "coordinator-cancel.db")
    pending = None
    try:
        bundle = BehaviorBundleService(db).ensure({"code": "test"})
        service = AgentTaskService(db)
        run = service.create_run("local-user", "goal", {}, bundle.id,
                                 expert_roles=("critic",), idempotency_key="cancel-synthesis")
        worker = ManagedAgentWorker(service, Model(), lease_seconds=30,
                                    safety_judge=Model() if stage == "safety" else None)
        await worker.run_once()
        if stage == "synthesis":
            await worker.run_once()
        pending = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), 2)
        service.cancel_run(run["id"], "cancel during synthesis")
        await asyncio.wait_for(cancelled.wait(), 2)
        await asyncio.wait_for(pending, 2)
        assert service.get_run(run["id"])["status"] == "CANCELLED"
        assert service.get_task(run["coordinator_task_id"])["result_artifact_id"] is None
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        db.close()


def test_worker_runs_three_experts_and_coordinator_merges_deterministically(tmp_path):
    db = Database(tmp_path / "agent.db")
    bundle = BehaviorBundleService(db).ensure({"code":"test"})
    service = AgentTaskService(db)
    run = service.create_run("local-user", "制定训练计划", {"goal":"训练"}, bundle.id, idempotency_key="expert-1")
    worker = ManagedAgentWorker(service, ExpertModel(), poll_interval=.001)
    for _ in range(5):
        assert asyncio.run(worker.run_once()) is True
    completed = service.get_run(run["id"])
    assert completed["status"] == "SUCCEEDED"
    result = service.artifact(service.get_task(completed["coordinator_task_id"])["result_artifact_id"])["content"]
    assert [item["role"] for item in result["experts"]] == ["critic", "planner", "researcher"]
    assert "综合" in result["summary"]


def test_coordinator_deterministically_stitches_claim_source_evidence():
    from app.agents import _append_source_evidence

    summary = _append_source_evidence("综合结论。", [
        {
            "role": "planner",
            "result": {
                "findings": [
                    {"text": "方案A总耗时130分钟，超过120分钟。", "source_refs": ["S1"]},
                    {"text": "方案B需要批准且状态未知。", "source_refs": ["S2"]},
                ],
            },
        },
        {
            "role": "researcher",
            "result": {
                "findings": [
                    {"text": "方案C的执行时间存在冲突。", "source_refs": ["S3", "S4"]},
                ],
            },
        },
    ])

    assert summary.startswith("综合结论。\n\n来源证据")
    assert "[S1] 方案A总耗时130分钟，超过120分钟。" in summary
    assert "[S2] 方案B需要批准且状态未知。" in summary
    assert "[S3, S4] 方案C的执行时间存在冲突。" in summary


def test_coordinator_persists_distinct_role_scoped_child_objectives(tmp_path):
    db = Database(tmp_path / "role-objectives.db")
    try:
        bundle = BehaviorBundleService(db).ensure({"code": "test"})
        service = AgentTaskService(db)
        run = service.create_run(
            "local-user", "比较候选方案", {}, bundle.id, idempotency_key="role-objectives",
        )
        worker = ManagedAgentWorker(service, ExpertModel())

        assert asyncio.run(worker.run_once()) is True

        root = service.get_task(run["coordinator_task_id"])
        children = {item["role"]: item for item in service.children(root["id"])}
        objectives = {role: item["objective"] for role, item in children.items()}
        assert set(objectives) == {"researcher", "planner", "critic"}
        assert len(set(objectives.values())) == 3
        assert all("比较候选方案" in objective for objective in objectives.values())
        assert all("角色限定（优先遵守）" in objective for objective in objectives.values())
        assert all(term in objectives["researcher"] for term in ("来源", "事实与推断", "证据不足", "source_refs"))
        assert "无需覆盖没有来源争议的完整方案" in objectives["researcher"]
        assert all(term in objectives["planner"] for term in ("时间", "顺序", "前置依赖", "可执行条件"))
        assert all(term in objectives["critic"] for term in ("反例", "矛盾", "约束违反", "错误执行承诺"))
    finally:
        db.close()


def test_completed_expert_run_projects_a_visible_thread_message_and_events(tmp_path):
    from app.conversation import ConversationService
    db = Database(tmp_path / "agent.db")
    conversation = ConversationService(db)
    thread = conversation.create_thread("专家")
    bundle = BehaviorBundleService(db).ensure({"code":"test"})
    service = AgentTaskService(db, thread_events=conversation.events)
    run = service.create_run("local-user", "制定训练计划", {"goal":"训练"}, bundle.id, thread_id=thread.id, idempotency_key="expert-visible")
    worker = ManagedAgentWorker(service, ExpertModel())
    for _ in range(5): assert asyncio.run(worker.run_once()) is True
    assistant = conversation.messages(thread.id)[-1]
    assert assistant.role == "assistant" and assistant.status == "ready"
    assert "专家协作结果" in assistant.content and "researcher" in assistant.content
    event_types = [event.type for event in conversation.events.list(thread.id)]
    assert "expert.run.queued" in event_types and "expert.run.completed" in event_types


def test_worker_uses_requested_expert_roles(tmp_path):
    db = Database(tmp_path / "agent.db")
    bundle = BehaviorBundleService(db).ensure({"code":"test"})
    service = AgentTaskService(db)
    run = service.create_run(
        "local-user", "compare options", {}, bundle.id, expert_roles=("planner", "critic"), idempotency_key="expert-roles",
    )
    worker = ManagedAgentWorker(service, ExpertModel())
    for _ in range(4):
        assert asyncio.run(worker.run_once()) is True
    result = service.artifact(service.get_task(run["coordinator_task_id"])["result_artifact_id"])["content"]
    assert [item["role"] for item in result["experts"]] == ["critic", "planner"]


def test_live_worker_routes_coordinator_synthesis_with_pinned_context(tmp_path):
    from types import SimpleNamespace
    from app.agents import LiveExpertModel

    class Gateway:
        control_store = object()
        def __init__(self): self.contexts=[]
        def set_call_context(self, context): self.contexts.append(context);return context
        def reset_call_context(self, token): pass
        async def complete(self, request, **kwargs):
            if request.role == "coordinator": return SimpleNamespace(message="综合结论")
            return SimpleNamespace(message='{"summary":"专家结论","findings":[],"risks":[],"open_questions":[]}')

    db=Database(tmp_path/"coordinator.db");bundle=BehaviorBundleService(db).ensure({"code":"test"})
    service=AgentTaskService(db);run=service.create_run("local-user","目标",{},bundle.id,idempotency_key="coordinator")
    gateway=Gateway();worker=ManagedAgentWorker(service,LiveExpertModel(gateway))
    for _ in range(5): assert asyncio.run(worker.run_once()) is True
    result=service.artifact(service.get_task(run["coordinator_task_id"])["result_artifact_id"])["content"]
    assert result["summary"]=="综合结论"
    context=gateway.contexts[-1]
    assert (context.role,context.agent_task_id,context.runtime_bundle_id)==("coordinator",run["coordinator_task_id"],bundle.id)


def test_worker_without_model_fails_children_without_faking_results(tmp_path):
    db = Database(tmp_path / "agent.db")
    bundle = BehaviorBundleService(db).ensure({"code":"test"})
    service = AgentTaskService(db)
    run = service.create_run("local-user", "goal", {"goal":"x"}, bundle.id, idempotency_key="expert-2")
    worker = ManagedAgentWorker(service, None)
    assert asyncio.run(worker.run_once()) is True
    for _ in range(3): assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is True
    assert service.get_run(run["id"])["status"] == "FAILED"
    assert service.get_task(run["coordinator_task_id"])["result_artifact_id"] is None


def test_managed_worker_executes_read_only_experts_concurrently(tmp_path):
    class SlowExpertModel(ExpertModel):
        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def execute(self, role, objective, context, inputs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(.05)
                return await super().execute(role, objective, context, inputs)
            finally:
                self.active -= 1

    async def scenario():
        db = Database(tmp_path / "parallel.db")
        bundle = BehaviorBundleService(db).ensure({"code":"test"})
        service = AgentTaskService(db)
        run = service.create_run("local-user", "goal", {}, bundle.id, idempotency_key="parallel")
        model = SlowExpertModel()
        worker = ManagedAgentWorker(service, model, poll_interval=.001, max_concurrency=3)
        await worker.start()
        try:
            while service.get_run(run["id"])["status"] not in {"SUCCEEDED", "FAILED"}:
                await asyncio.sleep(.01)
        finally:
            await worker.stop()
        return model.max_active

    assert asyncio.run(scenario()) == 3


def test_model_execution_heartbeats_and_stops_after_cancel(tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingModel(ExpertModel):
        async def execute(self, role, objective, context, inputs):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def scenario():
        db = Database(tmp_path / "cancel-heartbeat.db")
        bundle = BehaviorBundleService(db).ensure({"code":"test"})
        service = AgentTaskService(db)
        run = service.create_run("local-user", "goal", {}, bundle.id, idempotency_key="cancel-heartbeat")
        worker = ManagedAgentWorker(service, BlockingModel(), poll_interval=.001, lease_seconds=1, max_concurrency=3)
        await worker.start()
        try:
            await asyncio.wait_for(started.wait(), 1)
            service.cancel_run(run["id"], "stop")
            await asyncio.wait_for(cancelled.wait(), 1)
        finally:
            await worker.stop()

    asyncio.run(scenario())
