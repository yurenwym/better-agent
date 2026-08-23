from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.conversation import ConversationService
from app.db import Database
from app.research.models import ResearchEvent
from app.research.service import ResearchConflict, ResearchService


class CompletingEngine:
    async def run_research(self, request):
        yield ResearchEvent("plan", "planning", {"title": request.topic, "sections": ["结论"], "queries": [request.topic]})
        yield ResearchEvent("sources", "retrieving", {"count": 1})
        yield ResearchEvent("section", "writing", {"ordinal": 1, "heading": "结论", "markdown": "## 结论\n\n有证据。", "summary": "done"})
        yield ResearchEvent("report", "completed", {"title": request.topic, "markdown": "# 报告\n\n有证据。", "source_count": 1, "evidence_count": 1})


def build(tmp_path):
    db = Database(tmp_path / "agent.db")
    conversation = ConversationService(db)
    return db, conversation, ResearchService(db, conversation.events, CompletingEngine())


def test_create_job_is_idempotent_and_creates_anchor_and_streaming_message(tmp_path) -> None:
    db, conversation, service = build(tmp_path)
    thread = conversation.create_thread("研究")
    first = service.create_manual(thread.id, "研究 SQLite WAL", "client-1", ("web",))
    repeated = service.create_manual(thread.id, "研究 SQLite WAL", "client-1", ("web",))
    assert repeated.id == first.id
    assert first.status == "QUEUED" and first.phase == "queued"
    messages = conversation.messages(thread.id)
    assert len(messages) == 2
    assert messages[-1].status == "streaming"
    assert [event.type for event in conversation.events.list(thread.id)].count("research.queued") == 1


def test_manual_research_rejects_thread_with_active_turn(tmp_path) -> None:
    _, conversation, service = build(tmp_path)
    thread = conversation.create_thread()
    conversation.accept_turn(thread.id, "turn-1", "普通问题", [])
    with pytest.raises(ResearchConflict):
        service.create_manual(thread.id, "研究", "client-2", ("web",))


def test_claim_lease_takeover_and_old_owner_cannot_finalize(tmp_path) -> None:
    db, conversation, service = build(tmp_path)
    job = service.create_manual(conversation.create_thread().id, "研究", "client-3", ("web",))
    claimed = service.claim_next("worker-a", 30)
    assert claimed and claimed.id == job.id and claimed.attempts == 1
    with db.transaction() as connection:
        connection.execute("UPDATE research_jobs SET lease_until=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(), job.id))
    takeover = service.claim_next("worker-b", 30)
    assert takeover and takeover.attempts == 2
    with pytest.raises(PermissionError):
        service.complete(job.id, "worker-a", "wrong", "# Wrong", 1, 1)
    service.complete(job.id, "worker-b", "ok", "# Correct", 1, 1)
    snapshot = service.get(job.id)
    assert snapshot.status == "COMPLETED"
    with db.connection() as connection:
        attempts = connection.execute("SELECT status FROM research_job_attempts WHERE job_id=? ORDER BY attempt", (job.id,)).fetchall()
    assert [row[0] for row in attempts] == ["LEASE_LOST", "COMPLETED"]


def test_cancel_and_retry_are_idempotent_new_jobs(tmp_path) -> None:
    _, conversation, service = build(tmp_path)
    job = service.create_manual(conversation.create_thread().id, "old", "client-4", ("web",))
    assert service.cancel(job.id).status == "CANCELLED"
    assert service.cancel(job.id).status == "CANCELLED"
    retry = service.retry(job.id, "new", "retry-key")
    repeated = service.retry(job.id, "new", "retry-key")
    assert retry.id == repeated.id and retry.id != job.id and retry.retry_of_job_id == job.id

def test_retryable_failure_requeues_until_max_attempts(tmp_path):
 _,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","retryable",("web",));service.claim_next("w",30)
 queued=service.fail(job.id,"w","timeout",True);assert queued.status=="QUEUED"
