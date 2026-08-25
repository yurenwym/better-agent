import asyncio

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
