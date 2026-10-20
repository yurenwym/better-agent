"""R02 archive and embedding fixtures. Lifecycle events come from the real workers."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = str(ROOT / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from app.embedding import EmbeddingBatch
from app.embedding_worker import EmbeddingWorker
from app.memory_archive import ConversationArchiver
from app.trace_query import TraceQuery


async def _summary(payload):
    ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
    return {
        "synopsis": [{"text": "s", "source_message_ids": [ids[0]]}],
        "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
    }


class FixedProvider:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return EmbeddingBatch(
            tuple((1.0, *([0.0] * 1023)) for _ in texts),
            "Qwen/Qwen3-Embedding-4B",
            1024,
        )


def _coverage(database, thread_id: str) -> int:
    with database.connection() as connection:
        row = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
    return int(row["archived_through_seq"]) if row and row["archived_through_seq"] is not None else 0


def _embedding_job(database, revision_id: str):
    with database.connection() as connection:
        current = connection.execute(
            "SELECT current_revision_id FROM memory_entries WHERE current_revision_id=?",
            (revision_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT profile_id, content_hash, status FROM embedding_jobs WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
    return current, job


def _seed_embedding(runtime, thread_id, turn_id) -> dict:
    with runtime.db.connection() as connection:
        message = connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=? AND turn_id=? AND role='user' ORDER BY message_seq, id",
            (thread_id, turn_id),
        ).fetchone()
    assert message is not None
    entry = runtime.memory_store.remember(
        "local-user",
        "fact",
        "user",
        "",
        "R02 embedding source",
        "r02-embedding",
        source_refs=[{"source_type": "thread_message", "source_id": message["id"]}],
        source_thread_id=thread_id,
    )
    revision = entry.revision_id
    provider = FixedProvider()
    worker = EmbeddingWorker(runtime.db, provider, owner="r02-e2e")
    current, job = None, None
    for _ in range(20):
        current, job = _embedding_job(runtime.db, revision)
        if job is not None and job["status"] == "COMPLETED":
            break
        worker.run_once()
    else:
        current, job = _embedding_job(runtime.db, revision)
    assert current is not None and current["current_revision_id"] == revision
    assert job is not None and job["status"] == "COMPLETED" and job["profile_id"]
    profile = job["profile_id"]
    with runtime.db.connection() as connection:
        vector = connection.execute(
            "SELECT revision_id, profile_id, content_hash FROM memory_embeddings "
            "WHERE revision_id=? AND profile_id=?",
            (revision, profile),
        ).fetchone()
    assert vector is not None
    assert vector["revision_id"] == revision and vector["profile_id"] == profile
    assert vector["content_hash"] == job["content_hash"]
    assert provider.calls > 0
    view = TraceQuery(runtime.db).by_thread(thread_id, owner_id="local-user")
    claimed = [
        event for event in view["events"]
        if event["type"] == "embedding.claimed" and event["payload"].get("revision_id") == revision
    ]
    committed = [
        event for event in view["events"]
        if event["type"] == "embedding.committed" and event["payload"].get("revision_id") == revision
    ]
    assert len(claimed) == 1 and len(committed) == 1
    job_id = claimed[0]["payload"]["job_id"]
    assert committed[0]["payload"]["job_id"] == job_id
    assert committed[0]["payload"]["profile_id"] == profile
    assert committed[0]["payload"]["status"] == "COMPLETED"
    return {
        "claimed_event_id": claimed[0]["event_id"],
        "committed_event_id": committed[0]["event_id"],
        "job_id": job_id,
        "revision": revision,
        "profile": profile,
        "status": job["status"],
        "provider_calls": provider.calls,
    }


def _seed_learning(runtime, thread_id):
    from app.learning import LearningConflict, digest
    content = "R02_SENSITIVE_LEARNING_BODY_20261019"
    turn = runtime.conversation.accept_turn(thread_id, "r02-learning", content, [])
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id=?", (turn.turn_id,))
        connection.execute("UPDATE turn_jobs SET status='COMPLETED' WHERE turn_id=?", (turn.turn_id,))
        message = connection.execute(
            "SELECT id FROM thread_messages WHERE turn_id=? AND role='user'",
            (turn.turn_id,),
        ).fetchone()
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE learning_jobs SET status='SUSPENDED', reason='r02_fixture_isolation' "
            "WHERE owner_id=? AND source_kind='experience' AND status='QUEUED' AND dispatched_at IS NULL",
            ("local-user",),
        )
    service = runtime.learning
    old = service.policy()
    policy = service.configure(
        "local-user", expected_version=old["version"], paused=False, allowed_assets=["memory"],
        cycle_microusd=100, daily_microusd=200, monthly_microusd=1000,
    )
    period = runtime.costs.today_period()
    runtime.costs.set_budget("local-user", "DAILY", period, 10000)
    runtime.costs.set_budget("local-user", "MONTHLY", period[:7], 10000)
    job_id = service.enqueue("local-user", "thread_message", message["id"], digest(content), message["id"])
    job = service.claim()
    assert job is not None and job["id"] == job_id
    root_id = service.dispatch(job)
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE learning_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
            (job_id,),
        )
    assert service.claim() is None
    assert service.claim() is None
    try:
        service.dispatch(job)
    except LearningConflict:
        old_dispatch_rejected = True
    else:
        raise AssertionError("expired dispatched learning job sent again")
    with runtime.db.connection() as connection:
        current = connection.execute(
            "SELECT status,dispatched_at,root_budget_id FROM learning_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        roots = connection.execute(
            "SELECT COUNT(*) FROM task_budget_roots WHERE root_object_id=?",
            (job_id,),
        ).fetchone()[0]
    assert current["status"] == "UNKNOWN" and current["dispatched_at"] and current["root_budget_id"] == root_id and roots == 1
    events = [
        event for event in TraceQuery(runtime.db).by_thread(thread_id, owner_id="local-user")["events"]
        if event["payload"].get("job_id") == job_id and event["type"].startswith("learning.")
    ]
    claimed = [event for event in events if event["type"] == "learning.claimed"]
    recovered = [event for event in events if event["type"] == "learning.recovered"]
    assert len(claimed) == len(recovered) == 1
    assert recovered[0]["outcome"]["effect"] == "unknown" and recovered[0]["outcome"]["error"]["retryable"] is False
    assert content not in json.dumps(events)
    service.configure(
        "local-user", expected_version=policy["version"], paused=True, allowed_assets=["memory"],
        cycle_microusd=100, daily_microusd=200, monthly_microusd=1000,
    )
    return {
        "thread_id": thread_id,
        "turn_id": turn.turn_id,
        "job_id": job_id,
        "status": current["status"],
        "dispatched": bool(current["dispatched_at"]),
        "root_id": root_id,
        "root_count": roots,
        "old_dispatch_rejected": old_dispatch_rejected,
        "claimed_event_id": claimed[0]["event_id"],
        "recovered_event_id": recovered[0]["event_id"],
        "effect": recovered[0]["outcome"]["effect"],
        "retryable": recovered[0]["outcome"]["error"]["retryable"],
    }


def _insert_r02_report(database, job_id: str, thread_id: str, turn_id: str, now: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO research_jobs(id,thread_id,source_turn_id,trigger_kind,occurrence_key,topic,"
            "source_scopes_json,status,phase,available_at,created_at,updated_at) "
            "VALUES (?,?,?,'manual',?,'R02 report','[\"web\"]','COMPLETED','completed',?,?,?)",
            (job_id, thread_id, turn_id, "r02-report-" + job_id, now, now, now),
        )
        connection.execute(
            "INSERT INTO research_reports(job_id,title,markdown,partial_markdown,created_at,updated_at,completed_at) "
            "VALUES (?,?,?,'',?,?,?)",
            (job_id, "R02 report", "R02 safe body", now, now, now),
        )


async def _seed_notifications(runtime, thread_id):
    import os
    import uuid
    from datetime import datetime, timezone

    import httpx
    from app.notifications import NotificationService

    now = datetime.now(timezone.utc).isoformat()
    job_ids = {"sent": uuid.uuid4().hex, "unknown": uuid.uuid4().hex}
    previous = os.environ.get("R02_NOTIFICATION_KEY")
    os.environ["R02_NOTIFICATION_KEY"] = "r02-fixed-offline"
    try:
        results = {}
        for kind, job_id in job_ids.items():
            turn = runtime.conversation.accept_turn(thread_id, "r02-notification-" + kind, "R02 notification " + kind, [])
            with runtime.db.transaction() as connection:
                connection.execute("UPDATE turns SET status='COMPLETED' WHERE id=?", (turn.turn_id,))
                connection.execute("UPDATE turn_jobs SET status='COMPLETED' WHERE turn_id=?", (turn.turn_id,))
            _insert_r02_report(runtime.db, job_id, thread_id, turn.turn_id, now)
            calls = []

            def handler(request, kind=kind, calls=calls):
                calls.append(request)
                if kind == "sent":
                    return httpx.Response(200, json={"code": 0})
                raise httpx.ReadTimeout("offline uncertain", request=request)

            service = NotificationService(runtime.db, transport=httpx.MockTransport(handler))
            try:
                channel = service.create(
                    "R02 " + kind, "serverchan", "R02_NOTIFICATION_KEY", owner_id="local-user",
                )
                key = "report:" + job_id + ":" + channel.id
                delivery = await service.send(
                    channel.id, "R02 safe title", "R02 safe body", job_id,
                    owner_id="local-user", dispatch_key=key,
                )
                expected = "SENT" if kind == "sent" else "UNKNOWN"
                assert delivery.status == expected
                replay = await service.send(
                    channel.id, "R02 safe title", "R02 safe body", job_id,
                    owner_id="local-user", dispatch_key=key,
                )
                assert replay.id == delivery.id and len(calls) == 1
                if kind == "unknown":
                    blocked = await service.send(
                        channel.id, "R02 safe title", "R02 safe body", job_id,
                        owner_id="local-user", dispatch_key=key + ":unconfirmed",
                    )
                    assert blocked.id == delivery.id and blocked.status == "UNKNOWN" and len(calls) == 1
                with runtime.db.connection() as connection:
                    stored = connection.execute(
                        "SELECT id, status, job_id FROM notification_deliveries WHERE id=?",
                        (delivery.id,),
                    ).fetchone()
                assert stored is not None
                assert stored["id"] == delivery.id and stored["status"] == delivery.status and stored["job_id"] == job_id
                events = [
                    event for event in TraceQuery(runtime.db).by_thread(thread_id, owner_id="local-user")["events"]
                    if event["payload"].get("delivery_id") == delivery.id
                ]
                claimed = [event for event in events if event["type"] == "notification.claimed"]
                result_type = "notification.sent" if kind == "sent" else "notification.unknown"
                result = [event for event in events if event["type"] == result_type]
                assert len(claimed) == 1 and len(result) == 1
                if kind == "unknown":
                    assert result[0]["outcome"]["effect"] == "unknown" and result[0]["outcome"]["error"]["retryable"] is False
                results[kind] = {
                    "thread_id": thread_id,
                    "turn_id": turn.turn_id,
                    "report_job_id": job_id,
                    "channel_id": channel.id,
                    "delivery_id": stored["id"],
                    "status": stored["status"],
                    "provider_calls": len(calls),
                    "replay_same_delivery": replay.id == delivery.id,
                    "claimed_event_id": claimed[0]["event_id"],
                    "result_event_id": result[0]["event_id"],
                }
            finally:
                await service.transport.aclose()
        return results
    finally:
        if previous is None:
            os.environ.pop("R02_NOTIFICATION_KEY", None)
        else:
            os.environ["R02_NOTIFICATION_KEY"] = previous


def seed_worker_scenarios(runtime) -> dict:
    thread = runtime.conversation.create_thread("R02 Worker trace")
    turn = runtime.conversation.accept_turn(thread.id, "r02-archive", "archive source " + "u" * 2000, [])
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id=?", (turn.turn_id,))
        connection.execute("UPDATE turn_jobs SET status='COMPLETED' WHERE turn_id=?", (turn.turn_id,))
    before = _coverage(runtime.db, thread.id)
    assert before == 0
    archiver = ConversationArchiver(runtime.db, runtime.memory_store, _summary, keep_messages=0)
    job_id = archiver.enqueue(thread.id, source_turn_id=turn.turn_id)
    assert job_id is not None
    claim = archiver.claim(job_id)
    assert claim is not None
    asyncio.run(archiver.process(claim))
    after = _coverage(runtime.db, thread.id)
    assert after > 0
    with runtime.db.connection() as connection:
        job = connection.execute(
            "SELECT status FROM memory_archive_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        episode = connection.execute(
            "SELECT summary, synopsis_json, source_message_ids_json FROM memory_episodes WHERE thread_id=?",
            (thread.id,),
        ).fetchone()
    assert job is not None and job["status"] == "COMPLETED"
    assert episode is not None and episode["summary"]
    source_ids = json.loads(episode["source_message_ids_json"])
    synopsis = json.loads(episode["synopsis_json"])
    assert source_ids and any(
        isinstance(item, dict) and source_ids[0] in item.get("source_message_ids", [])
        for item in synopsis
    )
    view = TraceQuery(runtime.db).by_thread(thread.id, owner_id="local-user")
    claimed = [event for event in view["events"] if event["type"] == "archive.claimed"]
    committed = [event for event in view["events"] if event["type"] == "archive.committed"]
    assert len(claimed) == 1 and len(committed) == 1
    assert claimed[0]["payload"]["job_id"] == committed[0]["payload"]["job_id"] == job_id
    return {
        "archive": {
            "thread_id": thread.id,
            "turn_id": turn.turn_id,
            "job_id": job_id,
            "claimed_event_id": claimed[0]["event_id"],
            "committed_event_id": committed[0]["event_id"],
            "coverage_before": before,
            "coverage_after": after,
            "summary": episode["summary"],
            "source_message_ids": source_ids,
            "status": job["status"],
        },
        "embedding": _seed_embedding(runtime, thread.id, turn.turn_id),
        "learning": _seed_learning(runtime, thread.id),
        "notification": asyncio.run(_seed_notifications(runtime, thread.id)),
    }
