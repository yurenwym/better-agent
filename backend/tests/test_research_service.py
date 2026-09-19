from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.behavior import BehaviorBundleService
from app.conversation import ConversationService
from app.db import Database
from app.research.models import Evidence, ResearchEvent, Source
from app.research.service import ResearchConflict, ResearchService
from app.research.worker import ManagedResearchWorker
from app.research.engine import TopicCoverageError


class CompletingEngine:
    async def run_research(self, request):
        yield ResearchEvent("plan", "planning", {"title": request.topic, "sections": ["结论"], "queries": [request.topic]})
        yield ResearchEvent("sources", "retrieving", {"count": 1})
        yield ResearchEvent("section", "writing", {"ordinal": 1, "heading": "结论", "markdown": "## 结论\n\n有证据。", "summary": "done"})
        yield ResearchEvent("report", "completed", {"title": request.topic, "markdown": "# 报告\n\n有证据。", "source_count": 1, "evidence_count": 1})


class CoverageFailureEngine:
    async def run_research(self, request):
        raise TopicCoverageError("incomplete", ("岗位要求", "投递渠道"))
        yield


@pytest.mark.asyncio
async def test_requested_cancel_wins_over_model_failure(tmp_path):
    _, conversation, service = build(tmp_path)
    job = service.create_manual(conversation.create_thread().id, "cancel", "cancel-error", ("web",))
    class Engine:
        async def run_research(self, request):
            service.cancel(request.job_id)
            raise ValueError("model failure after cancel")
            yield
    service.engine = Engine()
    await ManagedResearchWorker(service).run_once()
    assert service.get(job.id).status == "CANCELLED"


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


def test_partial_delivery_is_terminal_and_persists_traceability(tmp_path) -> None:
    _, conversation, service = build(tmp_path)
    job = service.create_manual(conversation.create_thread().id, "研究", "partial", ("web",))
    service.claim_next("worker", 30)
    traceability = ({
        "requirement": "已支持项", "conclusion": "结论", "evidence_ids": ["e1"],
        "source_ids": ["s1"], "citation_source_ids": ["s1"],
        "source_versions": [{"source_id": "s1", "content_hash": "hash", "retrieved_at": "now"}],
        "supported": True,
    },)
    partial = service.complete_partial(
        job.id, "worker", "部分报告", "# 部分报告", 1, 1,
        traceability, ("缺失项",),
    )
    assert partial.status == "PARTIAL" and partial.phase == "partial"
    assert partial.missing_requirements == ("缺失项",)
    assert partial.traceability == traceability
    assert conversation.messages(job.thread_id)[-1].status == "ready"
    assert "research.partial" in [event.type for event in conversation.events.list(job.thread_id)]


def test_manual_research_pins_runtime_bundle_and_retry_preserves_it(tmp_path) -> None:
    db, conversation, service = build(tmp_path)
    bundles = BehaviorBundleService(db)
    bundle_id = bundles.ensure({"test": "research-bundle-pin"}).id
    bundles.activate("stable", bundle_id, "research-bundle-pin")
    thread = conversation.create_thread()
    first = service.create_manual(thread.id, "研究", "bundle-pin", ("web",))
    with db.connection() as connection:
        pinned = connection.execute(
            "SELECT runtime_bundle_id FROM turns WHERE id=?", (first.source_turn_id,),
        ).fetchone()["runtime_bundle_id"]
    assert pinned == bundle_id
    service.claim_next("worker", 30)
    service.fail(first.id, "worker", "permanent", retryable=False)
    retried = service.retry(first.id, None, "bundle-pin-retry")
    with db.connection() as connection:
        retry_pinned = connection.execute(
            "SELECT runtime_bundle_id FROM turns WHERE id=?", (retried.source_turn_id,),
        ).fetchone()["runtime_bundle_id"]
    assert retry_pinned == bundle_id


def test_exact_claim_does_not_consume_an_unrelated_queued_job(tmp_path) -> None:
    _, conversation, service = build(tmp_path)
    first = service.create_manual(conversation.create_thread().id, "first", "exact-first", ("web",))
    second = service.create_manual(conversation.create_thread().id, "second", "exact-second", ("web",))
    claimed = service.claim(second.id, "dedicated-worker", 30)
    assert claimed and claimed.id == second.id
    assert service.get(first.id).status == "QUEUED"


def test_recovery_accepts_citation_alias_without_source_prefix(tmp_path) -> None:
    db, conversation, service = build(tmp_path)
    job = service.create_manual(conversation.create_thread().id, "alias", "citation-alias", ("web",))
    service.claim_next("worker", 30)
    source_id = "source_abc123"
    source = Source(source_id, 1, "web", "https://example.com", None, "source", "quoted evidence", None, "now", .9, "hash")
    evidence = Evidence("evidence_alias", source_id, "quoted evidence", None, .9)
    service.apply_event(job.id, "worker", ResearchEvent("plan", "planning", {"title":"alias","sections":["answer"],"queries":["alias"]}))
    service.apply_event(job.id, "worker", ResearchEvent("sources", "retrieving", {"items":[source]}))
    service.apply_event(job.id, "worker", ResearchEvent("evidence", "distilling", {"items":[evidence]}))
    service.apply_event(job.id, "worker", ResearchEvent("section", "writing", {"ordinal":1,"heading":"answer","markdown":"## answer\n\nquoted evidence [[source:abc123]]","summary":"answer"}))
    sections, sources, evidence_rows, plan = service.recovery_context(job.id)
    assert sections and [item.id for item in sources] == [source_id]
    assert [item.id for item in evidence_rows] == [evidence.id]
    assert plan and plan.sections == ("answer",)


@pytest.mark.asyncio
async def test_worker_exposes_permanent_gateway_failure_kind(tmp_path) -> None:
    from app.model_gateway import GatewayError

    _, conversation, service = build(tmp_path)
    class Engine:
        async def run_research(self, request):
            raise GatewayError("payment required", "payment")
            yield
    service.engine = Engine()
    job = service.create_manual(conversation.create_thread().id, "x", "payment", ("web",))
    await ManagedResearchWorker(service, lease_seconds=30).run_once()
    failed = service.get(job.id)
    assert failed.status == "FAILED"
    assert failed.failure_reason_code == "payment"


def test_cancel_and_retry_are_idempotent_new_jobs(tmp_path) -> None:
    _, conversation, service = build(tmp_path)
    job = service.create_manual(conversation.create_thread().id, "old", "client-4", ("web",))
    assert service.cancel(job.id).status == "CANCELLED"
    assert service.cancel(job.id).status == "CANCELLED"
    retry = service.retry(job.id, "new", "retry-key")
    repeated = service.retry(job.id, "new", "retry-key")
    assert retry.id == repeated.id and retry.id != job.id and retry.retry_of_job_id == job.id

def test_delete_terminal_research_removes_artifacts_and_preserves_conversation(tmp_path):
 db,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"old","delete-job",("web",));service.cancel(job.id)
 retry=service.retry(job.id,"new","delete-retry")
 service.delete(job.id)
 with pytest.raises(KeyError):service.get(job.id)
 with db.connection() as c:
  assert c.execute("SELECT COUNT(*) FROM research_reports WHERE job_id=?",(job.id,)).fetchone()[0]==0
  assert c.execute("SELECT retry_of_job_id FROM research_jobs WHERE id=?",(retry.id,)).fetchone()[0] is None
  message=c.execute("SELECT research_job_id,content FROM thread_messages WHERE thread_id=? AND role='assistant' ORDER BY message_seq LIMIT 1",(job.thread_id,)).fetchone()
  assert message[0] is None and message[1]=="该深度研究记录已删除。"

def test_delete_rejects_an_active_research(tmp_path):
 _,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"active","active-delete",("web",))
 with pytest.raises(ResearchConflict,match="active"):
  service.delete(job.id)
 assert service.get(job.id).status=="QUEUED"

def test_retryable_failure_requeues_until_max_attempts(tmp_path):
 _,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","retryable",("web",));service.claim_next("w",30)
 queued=service.fail(job.id,"w","timeout",True);assert queued.status=="QUEUED"

def test_terminal_failure_exposes_only_a_stable_reason_code(tmp_path):
 _,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","failed-reason",("web",));service.claim_next("w",30)
 failed=service.fail(job.id,"w","unknowncitation",False)
 assert failed.failure_reason_code=="unknowncitation"

def test_expired_last_attempt_is_failed_instead_of_stuck(tmp_path):
 db,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","exhaust",("web",))
 with db.transaction() as c:c.execute("UPDATE research_jobs SET status='RUNNING',attempts=max_attempts,lease_owner='dead',lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",(job.id,))
 assert service.claim_next("new",30) is None
 assert service.get(job.id).status=="FAILED"
 assert conversation.messages(job.thread_id)[-1].status=="failed"
 assert conversation.events.list(job.thread_id)[-1].type=="message.completed"

def test_recovery_context_restores_sections_sources_and_evidence(tmp_path):
 db,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","recover",("web",));service.claim_next("w",30)
 source=Source("old-source",1,"web","https://example.com/old",None,"Old","body",None,"now",.8,"hash")
 evidence=Evidence("old-evidence",source.id,"fact",None,.9)
 service.apply_event(job.id,"w",ResearchEvent("sources","retrieving",{"count":1,"items":[source]}))
 service.apply_event(job.id,"w",ResearchEvent("evidence","distilling",{"count":1,"items":[evidence]}))
 service.apply_event(job.id,"w",ResearchEvent("section","writing",{"ordinal":1,"heading":"Old","markdown":"## Old\n\nfact [[source:old-source]]","summary":"old"}))
 sections,sources,evidence_items,plan=service.recovery_context(job.id)
 assert sections[1]["heading"]=="Old"
 assert [item.id for item in sources]==["old-source"]
 assert [item.id for item in evidence_items]==["old-evidence"]

def test_failed_research_keeps_collected_source_and_evidence_counts(tmp_path):
 db,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","failed-counts",("web",));service.claim_next("w",30)
 source=Source("source-one",1,"web","https://example.com/one",None,"One","body",None,"now",.8,"hash")
 evidence=Evidence("evidence-one",source.id,"fact",None,.9)
 service.apply_event(job.id,"w",ResearchEvent("sources","retrieving",{"count":1,"items":[source]}))
 service.apply_event(job.id,"w",ResearchEvent("evidence","distilling",{"count":1,"items":[evidence]}))
 failed=service.fail(job.id,"w","unknowncitation")
 assert (failed.source_count,failed.evidence_count)==(1,1)

def test_failed_research_persists_safe_failure_diagnostics(tmp_path):
 db,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","failed-details",("web",));service.claim_next("w",30)
 failed=service.fail(job.id,"w","topiccoverageerror",diagnostics={"missing_requirements":["岗位要求","投递渠道"]})
 assert failed.failure_details=={"missing_requirements":["岗位要求","投递渠道"]}

@pytest.mark.asyncio
async def test_worker_persists_engine_failure_diagnostics(tmp_path):
 _,conversation,service=build(tmp_path);service.engine=CoverageFailureEngine();job=service.create_manual(conversation.create_thread().id,"x","worker-details",("web",))
 await ManagedResearchWorker(service,lease_seconds=30).run_once()
 failed=service.get(job.id)
 assert failed.failure_reason_code=="topiccoverageerror"
 assert failed.failure_details=={"missing_requirements":["岗位要求","投递渠道"]}

def test_source_event_exposes_only_safe_retrieval_diagnostics(tmp_path):
 _,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"private query","safe-diagnostics",("web",));service.claim_next("w",30)
 diagnostics={"attempted_queries":2,"successful_queries":0,"raw_sources":0,"accepted_sources":0,"failure_counts":{"search_timeout":2}}
 service.apply_event(job.id,"w",ResearchEvent("sources","retrieving",{"count":0,"diagnostics":diagnostics}))
 event=conversation.events.list(job.thread_id)[-1]
 assert event.type=="research.sources_updated" and event.data["diagnostics"]==diagnostics
 serialized=str(event.data)
 assert "private query" not in serialized and "https://" not in serialized and "secret" not in serialized

def test_expired_cancelled_last_attempt_finishes_cancelled(tmp_path):
 db,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","cancel-exhaust",("web",))
 with db.transaction() as c:c.execute("UPDATE research_jobs SET status='RUNNING',attempts=max_attempts,lease_owner='dead',lease_until='2000-01-01T00:00:00+00:00',cancel_requested_at='2000-01-01T00:00:00+00:00' WHERE id=?",(job.id,))
 assert service.claim_next("new",30) is None
 assert service.get(job.id).status=="CANCELLED"
 assert conversation.messages(job.thread_id)[-1].status=="cancelled"
 assert conversation.events.list(job.thread_id)[-1].type=="message.completed"

def test_complete_rejects_a_concurrent_cancel_request(tmp_path):
 db,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","cancel-before-commit",("web",));service.claim_next("w",30)
 service.cancel(job.id)
 with pytest.raises(ResearchConflict,match="cancelled"):
  service.complete(job.id,"w","wrong","# Wrong",1,1)
 assert service.get(job.id).status=="RUNNING"

@pytest.mark.asyncio
async def test_worker_finishes_a_commit_race_as_cancelled(tmp_path):
 _,conversation,service=build(tmp_path);job=service.create_manual(conversation.create_thread().id,"x","cancel-race",("web",));original=service.complete
 def raced(job_id,*args,**kwargs):
  service.cancel(job_id);return original(job_id,*args,**kwargs)
 service.complete=raced
 await ManagedResearchWorker(service,lease_seconds=30).run_once()
 assert service.get(job.id).status=="CANCELLED"
