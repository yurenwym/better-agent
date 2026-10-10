from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.research.service import ResearchConflict, ResearchService
from app.agents import AgentTaskConflict, AgentTaskService
from app.behavior import BehaviorBundleService


@pytest.mark.parametrize("rival", ["complete", "cancel"])
def test_research_terminal_race_has_one_delivery(migrated_postgres_url, tmp_path, rival):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        conversation = ConversationService(db)
        thread = conversation.create_thread("terminal race")
        service = ResearchService(db, conversation.events)
        job = service.create_manual(thread.id, "topic", "one", ("web",))
        lease_1 = service.claim(job.id, "worker", 60)
        assert lease_1
        barrier = Barrier(2)

        def submit(action):
            barrier.wait(timeout=10)
            try:
                if action == "cancel":
                    return service.cancel(job.id).status
                return service.complete(job.id, "worker", "report", "# report", 0, 0, epoch=lease_1.lease_epoch).status
            except (PermissionError, ResearchConflict):
                return "refused"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ["complete", rival]))
        current = service.get(job.id)
        if current.status == "RUNNING":
            assert current.cancel_requested_at
            service.finish_cancelled(job.id, "worker", epoch=lease_1.lease_epoch)
        events = conversation.events.list(thread.id)
        terminal = [event for event in events if event.type in {"research.completed", "research.cancelled"}]
        assert len(terminal) == 1
        if rival == "complete":
            assert results == ["COMPLETED", "COMPLETED"]
        assert service.get(job.id).status in {"COMPLETED", "CANCELLED"}
        with pytest.raises((PermissionError, ResearchConflict)):
            service.complete(job.id, "worker", "late", "# late report", 0, 0, epoch=lease_1.lease_epoch)
        assert service.get(job.id).status in {"COMPLETED", "CANCELLED"}
        assert "# late report" not in str(conversation.messages(thread.id))
    finally:
        db.close()


@pytest.mark.parametrize("rival", ["complete", "cancel"])
def test_expert_terminal_race_preserves_one_result(migrated_postgres_url, tmp_path, rival):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        service = AgentTaskService(db)
        bundle = BehaviorBundleService(db).ensure({"code": "race"})
        run = service.create_run("local-user", "topic", {}, bundle.id, idempotency_key="race")
        task = service.claim_next("worker", 60)
        barrier = Barrier(2)

        def submit(action):
            barrier.wait(timeout=10)
            try:
                if action == "cancel":
                    return service.cancel_run(run["id"], "cancel")["status"]
                return service.complete(task["id"], "worker", task["lease_epoch"], "answer", {"text": "ok"})["status"]
            except (PermissionError, AgentTaskConflict):
                return "refused"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ["complete", rival]))
        status = service.get_run(run["id"])["status"]
        assert sorted(results) == sorted([status, "refused"] if rival == "cancel" else ["SUCCEEDED", "SUCCEEDED"])
        assert service.get_task(task["id"])["status"] == status
        terminal = [event for event in service.events(run["id"]) if event["type"] in {"agent.run.completed", "agent.run.cancelled"}]
        assert len(terminal) == 1
        with db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM agent_artifacts WHERE task_id=?", (task["id"],)).fetchone()[0]
        assert count == (1 if status == "SUCCEEDED" else 0)
    finally:
        db.close()


def test_two_expert_workers_cannot_claim_same_task(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        service = AgentTaskService(db)
        bundle = BehaviorBundleService(db).ensure({"code": "race"})
        run = service.create_run("local-user", "topic", {}, bundle.id, idempotency_key="claim")
        barrier = Barrier(2)

        def claim(owner):
            barrier.wait(timeout=10)
            return service.claim_next(owner, 60)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["first", "second"]))
        assert sum(result is not None for result in results) == 1
        assert service.get_task(run["coordinator_task_id"])["attempts"] == 1
    finally:
        db.close()


def test_goal_completion_and_cancel_compete_atomically(migrated_postgres_url, tmp_path):
    import asyncio
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.memory import MemoryService
    from app.runtime import AgentRuntime, MockModelGateway
    from app.tools import create_default_registry
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        events, approvals = EventStore(db), ApprovalService(db)
        runtime = AgentRuntime(db=db, events=events, plans=PlanVersionService(db), approvals=approvals,
            checkpoints=CheckpointStore(db), memory=MemoryService(db, events, tmp_path / "memory"),
            tools=create_default_registry(tmp_path, db=db, approval_service=approvals), model=MockModelGateway())
        run = asyncio.run(runtime.create_goal("goal", "race"))
        asyncio.run(runtime.handle_message(run.id, "start"))
        runtime._set_run_fields(run.id, state="REFLECTING")
        barrier = Barrier(2)

        def submit(action):
            barrier.wait(timeout=10)
            return asyncio.run(runtime.cancel(run.id) if action == "cancel" else runtime._reflect_locked(run.id))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(submit, ["complete", "cancel"]))
        terminal = [event for event in events.list(run.id) if event.type in {"run.completed", "run.cancelled"}]
        assert len(terminal) == 1
        expected = "COMPLETED" if terminal[0].type == "run.completed" else "CANCELLED"
        assert runtime.get_run(run.id).state == expected
        if expected == "CANCELLED":
            assert runtime.checkpoints.latest(run.id).state == "CANCELLED"
    finally:
        db.close()


def test_two_research_workers_cannot_claim_the_same_job(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        conversation = ConversationService(db)
        service = ResearchService(db, conversation.events)
        job = service.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
        barrier = Barrier(2)
        def claim(owner):
            barrier.wait(timeout=10)
            return service.claim_next(owner, 60)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["first", "second"]))
        assert len([result for result in results if result is not None]) == 1
        assert service.get(job.id).attempts == 1
    finally:
        db.close()
