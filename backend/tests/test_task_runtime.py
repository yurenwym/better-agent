from datetime import datetime, timedelta, timezone
import json

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.research.service import ResearchService
from app.task_runtime import TaskRuntime, TaskRef, TaskKind, LeaseLost, TaskCancelled


def test_reused_worker_name_cannot_revive_old_epoch(tmp_path):
    db = Database(tmp_path / "lease.db")
    try:
        conversation = ConversationService(db)
        service = ResearchService(db, conversation.events)
        job = service.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
        runtime = TaskRuntime(db)
        ref = TaskRef(TaskKind.RESEARCH, job.id)
        with db.transaction() as connection:
            first = runtime.claim(connection, ref, "same-worker", 30)
            assert runtime.claim(connection, ref, "second", 30) is None
            connection.execute("UPDATE research_jobs SET lease_until=? WHERE id=?",
                               ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job.id))
        with db.transaction() as connection:
            with pytest.raises(LeaseLost):
                runtime.heartbeat(connection, first.token, 30)
            second = runtime.claim(connection, ref, "same-worker", 30)
            assert second.token.epoch == first.token.epoch + 1
            assert second.number == first.number + 1
        with db.transaction() as connection:
            with pytest.raises(LeaseLost):
                runtime.finish(connection, first.token, "COMPLETED")
            assert runtime.request_cancel(connection, ref)
            assert not runtime.request_cancel(connection, ref)
            with pytest.raises(TaskCancelled):
                runtime.finish(connection, second.token, "COMPLETED")
            runtime.finish(connection, second.token, "CANCELLED")
        assert service.get(job.id).status == "CANCELLED"
    finally:
        db.close()


def test_denied_lease_decision_does_not_change_the_task(tmp_path):
    db = Database(tmp_path / "denied.db")
    try:
        conversation = ConversationService(db)
        service = ResearchService(db, conversation.events)
        job = service.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
        runtime = TaskRuntime(db)
        ref = TaskRef(TaskKind.RESEARCH, job.id)
        runtime._lease_allowed = lambda *args, **kwargs: False
        with db.transaction() as connection:
            assert runtime.claim(connection, ref, "worker", 30) is None
            assert runtime.recover_expired(connection, ref, "FAILED") is False
            assert runtime.request_cancel(connection, ref) is False
        assert service.get(job.id).status == "QUEUED"
        assert service.get(job.id).cancel_requested_at is None
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("context_state", ["valid", "denied", "missing", "malformed", "wrong_run"])
async def test_standalone_goal_requires_persisted_identity_and_policy(tmp_path, monkeypatch, context_state):
    from app import policy_engine
    from app.execution_context import create_root_context, serialize_context
    from app.policy_engine import ActionKind, PolicyAction, PolicyDecision, PolicyReason
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway())
    try:
        run = await runtime.create_goal("goal", "work")
        budget = dict(run.budget)
        budget["agent_loop_context"] = serialize_context(create_root_context(
            owner_id="business-owner", run_id="other-run" if context_state == "wrong_run" else run.id,
        ))
        if context_state == "missing":
            budget.pop("agent_loop_context")
        elif context_state == "malformed":
            budget["agent_loop_context"] = "not-json"
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE runs SET budget_json=? WHERE id=?", (json.dumps(budget), run.id))
        calls = []
        original = policy_engine.decide_request

        def decide(request):
            calls.append(request)
            if context_state == "denied":
                return PolicyDecision(PolicyAction.DENY, PolicyReason.CAPABILITY_NOT_ALLOWED)
            return original(request)

        monkeypatch.setattr(policy_engine, "decide_request", decide)
        tasks = TaskRuntime(runtime.db)
        with runtime.db.transaction() as connection:
            attempt = tasks.claim(connection, TaskRef(TaskKind.GOAL, run.id), "worker-owner", 30)
            if context_state == "valid":
                assert attempt is not None
                tasks.heartbeat(connection, attempt.token, 30)
                tasks.finish(connection, attempt.token, "QUEUED")
                assert [request.action for request in calls] == [
                    ActionKind.TASK_CLAIM, ActionKind.TASK_HEARTBEAT, ActionKind.TASK_COMMIT,
                ]
            else:
                assert attempt is None
                row = connection.execute("SELECT execution_epoch,execution_owner FROM runs WHERE id=?", (run.id,)).fetchone()
                assert row["execution_epoch"] == 0
                assert row["execution_owner"] is None
        if context_state in {"valid", "denied"}:
            assert calls
            assert all(request.subject.owner_id == request.resource.owner_id == "business-owner" for request in calls)
        else:
            assert calls == []
    finally:
        runtime.db.close()
