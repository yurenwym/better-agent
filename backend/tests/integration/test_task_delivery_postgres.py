from concurrent.futures import ThreadPoolExecutor
from threading import Event

from app.agents import AgentTaskService
from app.behavior import BehaviorBundleService
from app.conversation import ConversationService
from app.db import Database


def test_thread_delete_serializes_before_expert_delivery(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        conversation = ConversationService(db)
        thread = conversation.create_thread("delete race")
        service = AgentTaskService(db, thread_events=conversation.events)
        bundle = BehaviorBundleService(db).ensure({"code": "delete race"})
        run = service.create_run("local-user", "topic", {}, bundle.id,
                                 thread_id=thread.id, idempotency_key="race")
        task = service.claim_next("worker", 60)
        entered = Event()

        def complete():
            entered.set()
            try:
                service.complete(task["id"], "worker", task["lease_epoch"], "expert_synthesis", {"summary": "late"})
            except PermissionError:
                return "refused"
            return "published"

        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.transaction() as connection:
                connection.execute("SELECT id FROM threads WHERE id=? FOR UPDATE", (thread.id,)).fetchone()
                pending = pool.submit(complete)
                assert entered.wait(5)
                connection.execute("UPDATE threads SET deleted_at=clock_timestamp()::text WHERE id=?", (thread.id,))
            assert pending.result(timeout=10) == "refused"
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agent_artifacts WHERE task_id=?", (task["id"],)).fetchone()[0] == 0
    finally:
        db.close()
