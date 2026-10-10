import pytest

from app.behavior import BehaviorBundleService
from app.agents import AgentTaskService
from app.conversation import ConversationService
from app.db import Database


def test_deleted_thread_rejects_late_expert_delivery(tmp_path):
    db = Database(tmp_path / "delivery.db")
    try:
        conversation = ConversationService(db)
        thread = conversation.create_thread("expert")
        service = AgentTaskService(db, thread_events=conversation.events)
        bundle = BehaviorBundleService(db).ensure({"code": "delivery"})
        run = service.create_run("local-user", "topic", {}, bundle.id,
                                 thread_id=thread.id, idempotency_key="delivery")
        claimed = service.claim_next("worker", 60)
        conversation.delete_thread(thread.id)
        with pytest.raises(PermissionError, match="thread"):
            service.complete(claimed["id"], "worker", claimed["lease_epoch"], "expert_synthesis", {"summary": "late"})
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agent_artifacts WHERE task_id=?", (claimed["id"],)).fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM thread_messages WHERE thread_id=? AND content='late'", (thread.id,)).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["research", "expert"])
@pytest.mark.parametrize("revoked", [False, True])
def test_result_delivery_rechecks_persisted_model_sources(tmp_path, kind, revoked):
    from app.research.service import ResearchService
    db = Database(tmp_path / "sources.db")
    try:
        conversation = ConversationService(db)
        thread = conversation.create_thread("sources")
        if kind == "research":
            service = ResearchService(db, conversation.events)
            job = service.create_manual(thread.id, "topic", "delivery", ("web",))
            claimed = service.claim(job.id, "worker", 60)
            run_id, task_id = job.id, None
            complete = lambda: service.complete(job.id, "worker", "report", "late", 0, 0, epoch=claimed.lease_epoch)
        else:
            service = AgentTaskService(db, thread_events=conversation.events)
            bundle = BehaviorBundleService(db).ensure({"code": "delivery"})
            run = service.create_run("local-user", "topic", {}, bundle.id,
                                     thread_id=thread.id, idempotency_key="delivery")
            claimed = service.claim_next("worker", 60)
            run_id, task_id = run["id"], claimed["id"]
            complete = lambda: service.complete(task_id, "worker", claimed["lease_epoch"], "expert_synthesis", {"summary": "late"})
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO model_invocations(id,owner_id,run_id,agent_task_id,role,purpose,routing_policy_digest,"
                "route_snapshot_json,request_digest,tool_schema_digest,context_snapshot_digest,status,idempotency_key,created_at) "
                "VALUES ('response','local-user',?,?,'writer','write','d','{}','r','t','c','SUCCEEDED','response','2026-10-10')",
                (None if kind == "expert" else run_id, task_id),
            )
            connection.execute(
                "INSERT INTO learning_snapshots(id,owner_id,task_kind,task_id,assets_json,digest,created_at) "
                "VALUES ('source','local-user','invocation','response','[]','d','2026-10-10')"
            )
            if revoked:
                connection.execute("UPDATE learning_snapshots SET invalidated_at='2026-10-10' WHERE id='source'")
        if revoked:
            from app.learning import LearningConflict
            with pytest.raises(LearningConflict, match="revoked"):
                complete()
        else:
            complete()
        with db.connection() as connection:
            if kind == "research":
                row = connection.execute("SELECT status FROM research_jobs WHERE id=?", (run_id,)).fetchone()
                assert row["status"] == ("RUNNING" if revoked else "COMPLETED")
                report = connection.execute("SELECT markdown FROM research_reports WHERE job_id=?", (run_id,)).fetchone()
                assert bool(report["markdown"]) is not revoked
            else:
                assert connection.execute("SELECT COUNT(*) FROM agent_artifacts WHERE task_id=?", (task_id,)).fetchone()[0] == int(not revoked)
    finally:
        db.close()
