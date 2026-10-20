"""V3 learning on PostgreSQL: the parts SQLite cannot prove.

Three things only the authoritative backend can show:

* migration 45 really lands, so the routing gate's input is auditable there too;
* the append-only triggers are `reject_append_only_mutation()`, not a SQLite
  `RAISE(ABORT)`;
* "duplicate promotion = 0" is enforced by the partial unique index on the
  database, not by the application layer.

The remote seams (JEV, Learning LLM, judge) are stubbed exactly as in
`tests/test_learning_pipeline_v3.py`; everything else is the real harness.

Requires Docker (the pgvector service) or `TEST_DATABASE_URL`.
"""
import pytest

from app.learning_contract import TARGET_MEMORY
from app.learning_decision import LearningDecision
from app.learning_pipeline import MODE_ACTIVE, STATUS_APPLIED, build_pipeline
from pg02_learning_helpers import (
    build_pg_runtime,
    close_learning_runtimes,
    database,
    isolated_learning_env,
)

from tests.test_learning_pipeline_v3 import StubAgent, StubDecisions, enqueue, stub_budget
from tests.test_learning_targets import memory_draft


@pytest.fixture
def v3_runtime(tmp_path, database, monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)
    runtime = build_pg_runtime(tmp_path, database)
    runtime.learning.configure("local-user", expected_version=0, paused=False,
                               allowed_assets=["memory", "skill", "prompt"])
    yield runtime


def make_message(runtime, content="以后生产数据库不能由 Agent 自动重启，必须人工批准。"):
    thread = runtime.conversation.create_thread("v3-pg")
    runtime.conversation.accept_turn(thread.id, "one", content)
    with runtime.db.connection() as connection:
        return connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=? AND role='user'", (thread.id,)).fetchone()[0]


def promoted_decision() -> LearningDecision:
    return LearningDecision(learn=True, target=TARGET_MEMORY, subtype="", confidence=1.0, support=1.0,
                            importance=0.8, risk="low", reason_codes=("explicit_user_constraint",),
                            model_identity="stub:jev")


# ------------------------------------------------------------- migration 45

def test_the_support_column_exists_on_the_authoritative_backend(v3_runtime):
    """The gate reads `support`; an audit without it reads like a bypassed threshold."""
    with v3_runtime.db.connection() as connection:
        columns = {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='learning_decisions'"
        ).fetchall()}
    assert {"confidence", "support"} <= columns


def test_the_decision_audit_is_append_only_on_postgres(v3_runtime):
    message_id = make_message(v3_runtime)
    decider = StubDecisions(v3_runtime.db, promoted_decision())
    job_id = enqueue(v3_runtime, message_id)
    decider.decide(owner_id="local-user", job_id=job_id, experience={"id": message_id})

    for statement in (
        "UPDATE learning_decisions SET target='BEHAVIOR' WHERE job_id=%s",
        "DELETE FROM learning_decisions WHERE job_id=%s",
    ):
        with pytest.raises(Exception, match="append-only"):
            with v3_runtime.db.transaction() as connection:
                connection.execute(statement, (job_id,))


def test_a_promotion_is_unique_per_candidate_on_postgres(v3_runtime):
    """V3 §61 'duplicate promotion = 0', enforced by the index rather than by code."""
    message_id = make_message(v3_runtime)
    decider = StubDecisions(v3_runtime.db, promoted_decision())
    job_id = enqueue(v3_runtime, message_id)
    decision = decider.decide(owner_id="local-user", job_id=job_id, experience={"id": message_id})
    assert decision.target == TARGET_MEMORY

    with v3_runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO learning_promotions(id,owner_id,job_id,target,candidate_id,evaluation_id,outcome,"
            "gate_json,reason,detail_json,evidence_digest,idempotency_key,created_at) "
            "VALUES ('p1','local-user',%s,'MEMORY','cand-1','','PROMOTED','{}','','{}','d1','k1',"
            "'2026-09-21T00:00:00+00:00')", (job_id,))
    with pytest.raises(Exception, match="idx_learning_promotions_single_promotion"):
        with v3_runtime.db.transaction() as connection:
            connection.execute(
                "INSERT INTO learning_promotions(id,owner_id,job_id,target,candidate_id,evaluation_id,outcome,"
                "gate_json,reason,detail_json,evidence_digest,idempotency_key,created_at) "
                "VALUES ('p2','local-user',%s,'MEMORY','cand-1','','PROMOTED','{}','','{}','d2','k2',"
                "'2026-09-21T00:00:01+00:00')", (job_id,))


# ------------------------------------------------------------ a whole cycle

def test_a_memory_cycle_runs_end_to_end_on_postgres(v3_runtime, monkeypatch):
    message_id = make_message(v3_runtime)
    job_id = enqueue(v3_runtime, message_id)
    stub_budget(monkeypatch, v3_runtime)

    pipeline = build_pipeline(
        v3_runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(v3_runtime.db, promoted_decision()),
        agent=StubAgent(lambda evidence_ids, source_refs: memory_draft(message_id)),
    )
    v3_runtime.learning.pipeline = pipeline

    assert v3_runtime.learning.run_once() is True
    job = v3_runtime.learning.history()[0]
    assert job["id"] == job_id and job["status"] == STATUS_APPLIED

    entries = v3_runtime.memory_store.list_entries()
    assert entries, "an ACTIVE memory promotion must reach the store"
    with v3_runtime.db.connection() as connection:
        stored = connection.execute(
            "SELECT target,learn,support FROM learning_decisions WHERE job_id=%s", (job_id,)).fetchone()
    assert tuple(stored) == (TARGET_MEMORY, 1, 1.0)


def test_concurrent_learning_skill_versions_on_postgres(v3_runtime):
    from concurrent.futures import ThreadPoolExecutor
    from app.learning_targets import build_adapters
    from tests.test_learning_targets import skill_draft
    jobs = [v3_runtime.learning.enqueue("local-user", "experience", f"concurrent-{i}", str(i), str(i)) for i in range(4)]
    adapter = build_adapters(db=v3_runtime.db, memory=v3_runtime.memory_store, platform=v3_runtime.skill_platform,
                             bundles=v3_runtime.behavior, evolution=v3_runtime.evolution)["SKILL"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        candidates = list(pool.map(lambda job: adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=job), jobs))
    assert len({candidate["version_id"] for candidate in candidates}) == 1
    changed = adapter.create_candidate(skill_draft(change={"description": "second round"}), owner_id="local-user",
        job_id=v3_runtime.learning.enqueue("local-user", "experience", "round2", "round2", "round2"))
    assert changed["version_id"] != candidates[0]["version_id"]


def _budgets(runtime):
    runtime.learning.configure(
        "local-user", expected_version=1, paused=False, allowed_assets=["memory", "skill", "prompt"],
        cycle_microusd=100, daily_microusd=200, monthly_microusd=1000,
    )


def _expire(runtime, job_id):
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE learning_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=%s",
            (job_id,),
        )


def _thread_id(runtime, message_id):
    with runtime.db.connection() as connection:
        return connection.execute(
            "SELECT thread_id FROM thread_messages WHERE id=%s", (message_id,),
        ).fetchone()["thread_id"]


def test_paused_policy_blocks_send_and_adoption(v3_runtime):
    from app.learning import LearningConflict

    message_id = make_message(v3_runtime)
    enqueue(v3_runtime, message_id)
    job = v3_runtime.learning.claim()
    v3_runtime.learning.configure(
        "local-user", expected_version=1, paused=True, allowed_assets=["memory", "skill", "prompt"],
    )
    agent = StubAgent(lambda evidence_ids, source_refs: memory_draft(message_id))
    pipeline = build_pipeline(
        v3_runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(v3_runtime.db, promoted_decision()), agent=agent,
    )
    with pytest.raises(LearningConflict, match="paused or changed"):
        pipeline.run_job(job)
    assert agent.calls == 0
    assert v3_runtime.memory_store.list_entries() == []
    assert v3_runtime.learning.history()[0]["status"] == "RUNNING"


def test_source_change_blocks_send_and_adoption(v3_runtime, monkeypatch):
    message_id = make_message(v3_runtime)
    job_id = enqueue(v3_runtime, message_id)
    with v3_runtime.db.transaction() as connection:
        connection.execute("UPDATE thread_messages SET content='changed after enqueue' WHERE id=%s", (message_id,))
    sends = {"count": 0}

    def dispatch(job):
        sends["count"] += 1
        return "offline-budget"

    monkeypatch.setattr(v3_runtime.learning, "dispatch", dispatch)
    monkeypatch.setattr(v3_runtime.learning, "assert_learning_call_allowed", lambda owner, root_id: True)
    agent = StubAgent(lambda evidence_ids, source_refs: memory_draft(message_id))
    pipeline = build_pipeline(
        v3_runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(v3_runtime.db, promoted_decision()), agent=agent,
    )
    v3_runtime.learning.pipeline = pipeline
    assert v3_runtime.learning.run_once() is True
    job = next(item for item in v3_runtime.learning.history() if item["id"] == job_id)
    assert job["status"] == "REJECTED"
    assert sends["count"] == 0 and agent.calls == 0
    assert v3_runtime.memory_store.list_entries() == []


def test_expired_lease_cannot_commit_and_restart_recovers_the_saved_owner(v3_runtime):
    from app.learning import LearningConflict
    from app.trace_query import TraceQuery

    message_id = make_message(v3_runtime)
    job_id = enqueue(v3_runtime, message_id)
    job = v3_runtime.learning.claim()
    assert job["owner_id"] == "local-user"
    _expire(v3_runtime, job_id)
    with pytest.raises(LearningConflict, match="lease lost"):
        v3_runtime.learning.finish(job, "APPLIED")
    with pytest.raises(LearningConflict, match="lease lost"):
        v3_runtime.learning.dispatch(job)
    recovered = v3_runtime.learning.claim()
    assert recovered["id"] == job_id and recovered["owner_id"] == "local-user"
    assert recovered["lease_token"] != job["lease_token"]
    with pytest.raises(LearningConflict, match="lease lost"):
        v3_runtime.learning.finish(job, "APPLIED")
    view = TraceQuery(v3_runtime.db).by_thread(_thread_id(v3_runtime, message_id), owner_id="local-user")
    kinds = [event["type"] for event in view["events"] if event["type"].startswith("learning.")]
    assert kinds == ["learning.claimed", "learning.recovered", "learning.claimed"]
    assert "learning" in view["sources"]["connected"]
    assert "learning" not in view["sources"]["deferred"]
    recovered_event = next(event for event in view["events"] if event["type"] == "learning.recovered")
    assert recovered_event["source"] == "learning"
    assert recovered_event["payload"]["status"] == "QUEUED"
    assert recovered_event["payload"]["owner_id"] == "local-user"
    assert recovered_event["outcome"]["status"] == "failed"
    assert recovered_event["outcome"]["effect"] == "not_applicable"
    assert recovered_event["outcome"]["reconciliation_ref"] is None
    assert recovered_event["outcome"]["error"]["retryable"] is True


def test_dispatched_job_restarts_as_unknown_without_another_send(v3_runtime):
    from app.learning import LearningConflict
    from app.trace_query import TraceQuery

    _budgets(v3_runtime)
    message_id = make_message(v3_runtime)
    job_id = enqueue(v3_runtime, message_id)
    job = v3_runtime.learning.claim()
    root_id = v3_runtime.learning.dispatch(job)
    _expire(v3_runtime, job_id)
    assert v3_runtime.learning.claim() is None
    with pytest.raises(LearningConflict, match="lease lost"):
        v3_runtime.learning.dispatch(job)
    current = next(item for item in v3_runtime.learning.history() if item["id"] == job_id)
    assert current["status"] == "UNKNOWN" and current["dispatched_at"]
    with v3_runtime.db.connection() as connection:
        roots = connection.execute(
            "SELECT COUNT(*) FROM task_budget_roots WHERE root_object_id=%s", (job_id,),
        ).fetchone()[0]
    assert roots == 1 and root_id
    view = TraceQuery(v3_runtime.db).by_thread(_thread_id(v3_runtime, message_id), owner_id="local-user")
    recovered_event = next(event for event in view["events"] if event["type"] == "learning.recovered")
    assert recovered_event["payload"]["status"] == "UNKNOWN"
    assert recovered_event["payload"]["dispatched"] is True
    assert recovered_event["outcome"]["status"] == "reconciliation_required"
    assert recovered_event["outcome"]["effect"] == "unknown"
    assert recovered_event["outcome"]["reconciliation_ref"] == job_id
    assert recovered_event["outcome"]["error"]["retryable"] is False


def test_checkpoint_resume_does_not_send_or_adopt_again(v3_runtime, monkeypatch):
    message_id = make_message(v3_runtime)
    enqueue(v3_runtime, message_id)
    sends = {"count": 0}

    def dispatch(job):
        sends["count"] += 1
        return "offline-budget"

    monkeypatch.setattr(v3_runtime.learning, "dispatch", dispatch)
    monkeypatch.setattr(v3_runtime.learning, "assert_learning_call_allowed", lambda owner, root_id: True)
    agent = StubAgent(lambda evidence_ids, source_refs: memory_draft(message_id))
    pipeline = build_pipeline(
        v3_runtime, mode="SHADOW",
        decisions=StubDecisions(v3_runtime.db, promoted_decision()), agent=agent,
    )
    first = pipeline.run_once()
    assert first is not None and sends["count"] == 1 and agent.calls == 1
    import json

    checkpoint = dict(first.observability)
    checkpoint["outcome"] = "PENDING_APPROVAL"
    with v3_runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE learning_jobs SET checkpoint_json=%s WHERE id=%s",
            (json.dumps(checkpoint, ensure_ascii=False), first.job_id),
        )
    job = next(item for item in v3_runtime.learning.history() if item["id"] == first.job_id)
    pipeline.resume(first.job_id, owner_id=job["owner_id"], expected_version=job["version"])
    assert sends["count"] == 1 and agent.calls == 1
    assert v3_runtime.memory_store.list_entries() == []


def test_concurrent_adoption_sends_and_adopts_once(v3_runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _budgets(v3_runtime)
    message_id = make_message(v3_runtime)
    job_id = enqueue(v3_runtime, message_id)
    job = v3_runtime.learning.claim()
    agent = StubAgent(lambda evidence_ids, source_refs: memory_draft(message_id))
    pipeline = build_pipeline(
        v3_runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(v3_runtime.db, promoted_decision()), agent=agent,
    )
    barrier = Barrier(2)

    def adopt(_name):
        barrier.wait()
        try:
            return pipeline.run_job(job)
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(adopt, ("a", "b")))
    assert agent.calls == 1
    applied = [item for item in v3_runtime.learning.history() if item["id"] == job_id and item["status"] == "APPLIED"]
    assert len(applied) == 1
    assert len(v3_runtime.memory_store.list_entries()) == 1
    assert sum(not isinstance(item, Exception) for item in results) == 1


def _promoted_count(runtime) -> int:
    with runtime.db.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM learning_promotions WHERE outcome='PROMOTED'",
        ).fetchone()[0]


def _accepted_proposals(runtime) -> int:
    with runtime.db.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM memory_proposals WHERE status='ACCEPTED'",
        ).fetchone()[0]


def _assert_not_adopted(runtime, job_id: str) -> None:
    job = next(item for item in runtime.learning.history() if item["id"] == job_id)
    assert job["status"] == "RUNNING"
    assert runtime.memory_store.list_entries() == []
    assert _promoted_count(runtime) == 0
    assert _accepted_proposals(runtime) == 0


def test_lease_lost_during_generation_does_not_adopt(v3_runtime):
    from app.learning import LearningConflict

    _budgets(v3_runtime)
    message_id = make_message(v3_runtime)
    enqueue(v3_runtime, message_id)
    job = v3_runtime.learning.claim()

    def draft(evidence_ids, source_refs):
        _expire(v3_runtime, job["id"])
        return memory_draft(message_id)

    agent = StubAgent(draft)
    pipeline = build_pipeline(
        v3_runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(v3_runtime.db, promoted_decision()), agent=agent,
    )
    with pytest.raises(LearningConflict, match="lease lost"):
        pipeline.run_job(job)
    assert agent.calls == 1
    _assert_not_adopted(v3_runtime, job["id"])


def test_policy_paused_during_generation_does_not_adopt(v3_runtime):
    from app.learning import LearningConflict

    _budgets(v3_runtime)
    message_id = make_message(v3_runtime)
    enqueue(v3_runtime, message_id)
    job = v3_runtime.learning.claim()

    def draft(evidence_ids, source_refs):
        v3_runtime.learning.configure(
            "local-user", expected_version=2, paused=True, allowed_assets=["memory", "skill", "prompt"],
            cycle_microusd=100, daily_microusd=200, monthly_microusd=1000,
        )
        return memory_draft(message_id)

    agent = StubAgent(draft)
    pipeline = build_pipeline(
        v3_runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(v3_runtime.db, promoted_decision()), agent=agent,
    )
    with pytest.raises(LearningConflict, match="paused or changed"):
        pipeline.run_job(job)
    assert agent.calls == 1
    _assert_not_adopted(v3_runtime, job["id"])


def test_expired_lease_during_adoption_lock_wait_does_not_adopt(v3_runtime, migrated_postgres_url):
    import threading
    import time

    import psycopg
    from app.learning import LearningConflict
    from app.learning_contract import TARGET_MEMORY

    _budgets(v3_runtime)
    message_id = make_message(v3_runtime)
    enqueue(v3_runtime, message_id)
    job = v3_runtime.learning.claim()
    agent = StubAgent(lambda evidence_ids, source_refs: memory_draft(message_id))
    pipeline = build_pipeline(
        v3_runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(v3_runtime.db, promoted_decision()), agent=agent,
    )
    proposal_ready = threading.Event()
    lock_held = threading.Event()
    release = threading.Event()
    found: dict[str, str] = {}
    box: dict[str, object] = {}
    adapter = pipeline.adapters[TARGET_MEMORY]
    original = adapter.create_candidate

    def wrapped(draft, *, owner_id, job_id, **kwargs):
        result = original(draft, owner_id=owner_id, job_id=job_id, **kwargs)
        found["proposal_id"] = result["proposal_id"]
        proposal_ready.set()
        assert lock_held.wait(30)
        return result

    adapter.create_candidate = wrapped

    def run() -> None:
        try:
            box["result"] = pipeline.run_job(job)
        except Exception as exc:
            box["error"] = exc

    def hold() -> None:
        with psycopg.connect(migrated_postgres_url) as connection:
            connection.execute("SET application_name = 'learning-adoption-holder'")
            connection.execute("BEGIN")
            try:
                connection.execute(
                    "SELECT id FROM memory_proposals WHERE id = %s FOR UPDATE",
                    (found["proposal_id"],),
                )
                lock_held.set()
                assert release.wait(30)
            finally:
                connection.rollback()

    worker = threading.Thread(target=run)
    holder = threading.Thread(target=hold)
    worker.start()
    try:
        assert proposal_ready.wait(30)
        holder.start()
        assert lock_held.wait(30)
        assert _proposal_lock_wait(migrated_postgres_url)
        _expire(v3_runtime, job["id"])
    finally:
        release.set()
        holder.join(30)
        worker.join(30)
    assert isinstance(box.get("error"), LearningConflict)
    assert "lease lost" in str(box["error"])
    assert agent.calls == 1
    _assert_not_adopted(v3_runtime, job["id"])


def _proposal_lock_wait(url: str) -> bool:
    import time

    import psycopg

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with psycopg.connect(url) as connection:
            waiting = connection.execute(
                """
                SELECT 1 FROM pg_stat_activity
                WHERE datname = current_database() AND state = 'active' AND wait_event_type = 'Lock'
                  AND query LIKE '%memory_proposals%' AND query LIKE '%FOR UPDATE%'
                  AND application_name <> 'learning-adoption-holder'
                LIMIT 1
                """
            ).fetchone()
        if waiting is not None:
            return True
        time.sleep(0.05)
    return False


def test_checkpoint_resume_rejects_an_expired_lease_before_adoption(v3_runtime, monkeypatch):
    from app.learning import LearningConflict

    message_id = make_message(v3_runtime)
    enqueue(v3_runtime, message_id)
    sends = {"count": 0}

    def dispatch(job):
        sends["count"] += 1
        return "offline-budget"

    monkeypatch.setattr(v3_runtime.learning, "dispatch", dispatch)
    monkeypatch.setattr(v3_runtime.learning, "assert_learning_call_allowed", lambda owner, root_id: True)
    agent = StubAgent(lambda evidence_ids, source_refs: memory_draft(message_id))
    pipeline = build_pipeline(
        v3_runtime, mode="SHADOW",
        decisions=StubDecisions(v3_runtime.db, promoted_decision()), agent=agent,
    )
    first = pipeline.run_once()
    assert first is not None
    import json

    checkpoint = dict(first.observability)
    checkpoint["outcome"] = "PENDING_APPROVAL"
    with v3_runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE learning_jobs SET checkpoint_json=%s WHERE id=%s",
            (json.dumps(checkpoint, ensure_ascii=False), first.job_id),
        )
    stored = next(item for item in v3_runtime.learning.history() if item["id"] == first.job_id)
    pipeline.set_mode(MODE_ACTIVE)
    original = pipeline.gate.promote

    def expire_then_promote(*args, **kwargs):
        _expire(v3_runtime, first.job_id)
        return original(*args, **kwargs)

    pipeline.gate.promote = expire_then_promote
    with pytest.raises(LearningConflict, match="lease lost"):
        pipeline.resume(first.job_id, owner_id=stored["owner_id"], expected_version=stored["version"])
    assert sends["count"] == 1 and agent.calls == 1
    assert v3_runtime.memory_store.list_entries() == []
    assert _promoted_count(v3_runtime) == 0
    assert _accepted_proposals(v3_runtime) == 0


def _skill_state(runtime, skill_id, version_id):
    with runtime.db.connection() as connection:
        skill = connection.execute(
            "SELECT status, default_version_id FROM skills WHERE id=%s", (skill_id,),
        ).fetchone()
        version = connection.execute(
            "SELECT status FROM skill_versions WHERE id=%s", (version_id,),
        ).fetchone()
    return skill["status"], skill["default_version_id"], version["status"]


def _pending_skill(runtime, monkeypatch):
    from app.learning_decision import LearningDecision
    from app.learning_eval import EvaluationResult
    from tests.test_learning_targets import skill_draft

    _budgets(runtime)
    message_id = make_message(runtime)
    enqueue(runtime, message_id)
    decision = LearningDecision(
        learn=True, target="SKILL", subtype="", confidence=1, support=1,
        importance=0.8, risk="low", reason_codes=(), model_identity="test",
    )
    agent = StubAgent(lambda evidence_ids, source_refs: skill_draft())
    pipeline = build_pipeline(
        runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(runtime.db, decision), agent=agent,
    )

    def evaluate(adapter, candidate, *, owner_id):
        return EvaluationResult(
            target="SKILL", candidate_id=candidate["candidate_id"], rule_pass=True,
            rule_checks={"no_grant": True, "no_requested_tools": True, "no_connectors": True},
            judge={"wins": 1, "losses": 0, "ties": 0}, outcome="PASS", reason="", metrics={},
        )

    monkeypatch.setattr(pipeline, "_evaluate", evaluate)
    first = pipeline.run_once()
    assert first is not None and first.outcome == "PENDING_APPROVAL"
    assert agent.calls == 1
    candidate = first.observability["candidate"]
    status, default_version, version_status = _skill_state(runtime, candidate["skill_id"], candidate["version_id"])
    assert status != "ENABLED" and version_status != "ENABLED"
    assert default_version == candidate["base_version_id"]
    assert _promoted_count(runtime) == 0
    stored = next(item for item in runtime.learning.history() if item["id"] == first.job_id)
    return pipeline, agent, first, stored, candidate


def _assert_skill_not_adopted(runtime, candidate) -> None:
    status, default_version, version_status = _skill_state(runtime, candidate["skill_id"], candidate["version_id"])
    assert status != "ENABLED"
    assert version_status != "ENABLED"
    assert default_version == candidate["base_version_id"]
    assert _promoted_count(runtime) == 0


def test_skill_resume_approval_enables_once(v3_runtime, monkeypatch):
    pipeline, agent, first, stored, candidate = _pending_skill(v3_runtime, monkeypatch)
    result = pipeline.resume(first.job_id, owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert result.outcome == "PROMOTED" and result.status == "APPLIED"
    assert agent.calls == 1
    status, default_version, version_status = _skill_state(v3_runtime, candidate["skill_id"], candidate["version_id"])
    assert status == "ENABLED" and version_status == "ENABLED"
    assert default_version == candidate["version_id"]
    assert _promoted_count(v3_runtime) == 1


def test_expired_skill_lease_before_resume_adoption_does_not_enable(v3_runtime, monkeypatch):
    from app.learning import LearningConflict

    pipeline, agent, first, stored, candidate = _pending_skill(v3_runtime, monkeypatch)
    adapter = pipeline.adapters["SKILL"]
    original = adapter.promote

    def expire_then_enable(candidate_row, *, owner_id, **kwargs):
        _expire(v3_runtime, first.job_id)
        return original(candidate_row, owner_id=owner_id, **kwargs)

    adapter.promote = expire_then_enable
    with pytest.raises(LearningConflict, match="lease lost"):
        pipeline.resume(first.job_id, owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert agent.calls == 1
    _assert_skill_not_adopted(v3_runtime, candidate)
    job = next(item for item in v3_runtime.learning.history() if item["id"] == first.job_id)
    assert job["status"] == "RUNNING"


def test_paused_policy_before_skill_resume_adoption_does_not_enable(v3_runtime, monkeypatch):
    from app.learning import LearningConflict

    pipeline, agent, first, stored, candidate = _pending_skill(v3_runtime, monkeypatch)
    adapter = pipeline.adapters["SKILL"]
    original = adapter.promote

    def pause_then_enable(candidate_row, *, owner_id, **kwargs):
        v3_runtime.learning.configure(
            "local-user", expected_version=2, paused=True, allowed_assets=["memory", "skill", "prompt"],
            cycle_microusd=100, daily_microusd=200, monthly_microusd=1000,
        )
        return original(candidate_row, owner_id=owner_id, **kwargs)

    adapter.promote = pause_then_enable
    with pytest.raises(LearningConflict, match="paused or changed"):
        pipeline.resume(first.job_id, owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert agent.calls == 1
    _assert_skill_not_adopted(v3_runtime, candidate)
    job = next(item for item in v3_runtime.learning.history() if item["id"] == first.job_id)
    assert job["status"] == "RUNNING"


def test_expired_lease_during_skill_lock_wait_does_not_enable(v3_runtime, migrated_postgres_url, monkeypatch):
    import threading

    import psycopg
    from app.learning import LearningConflict

    pipeline, agent, first, stored, candidate = _pending_skill(v3_runtime, monkeypatch)
    held = threading.Event()
    release = threading.Event()
    box: dict[str, object] = {}

    def resume() -> None:
        try:
            box["result"] = pipeline.resume(
                first.job_id, owner_id=stored["owner_id"], expected_version=stored["version"], approve=True,
            )
        except Exception as exc:
            box["error"] = exc

    def hold() -> None:
        connection = psycopg.connect(migrated_postgres_url)
        try:
            connection.execute("SET application_name = 'learning-skill-holder'")
            connection.execute("SELECT id FROM skills WHERE id = %s FOR UPDATE", (candidate["skill_id"],))
            held.set()
            assert release.wait(30)
        finally:
            connection.rollback()
            connection.close()

    holder = threading.Thread(target=hold)
    worker = threading.Thread(target=resume)
    holder.start()
    try:
        assert held.wait(30)
        worker.start()
        assert _skill_lock_wait(migrated_postgres_url)
        _expire(v3_runtime, first.job_id)
    finally:
        release.set()
        holder.join(40)
        worker.join(40)
    assert isinstance(box.get("error"), LearningConflict)
    assert "lease lost" in str(box["error"])
    assert agent.calls == 1
    _assert_skill_not_adopted(v3_runtime, candidate)


def _skill_lock_wait(url: str) -> bool:
    import time

    import psycopg

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with psycopg.connect(url) as connection:
            waiting = connection.execute(
                """
                SELECT 1 FROM pg_stat_activity
                WHERE datname = current_database() AND state = 'active' AND wait_event_type = 'Lock'
                  AND query LIKE '%skills%' AND query LIKE '%FOR UPDATE%'
                  AND application_name <> 'learning-skill-holder'
                LIMIT 1
                """
            ).fetchone()
        if waiting is not None:
            return True
        time.sleep(0.05)
    return False


def _behavior_state(runtime, candidate_id):
    with runtime.db.connection() as connection:
        approvals = connection.execute(
            "SELECT COUNT(*) FROM evolution_decisions WHERE candidate_id=%s AND decision='APPROVE'",
            (candidate_id,),
        ).fetchone()[0]
        deployment = connection.execute(
            "SELECT status FROM canary_deployments WHERE candidate_id=%s ORDER BY created_at DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        stable = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()["bundle_id"]
        outcomes = [row["outcome"] for row in connection.execute(
            "SELECT outcome FROM learning_promotions WHERE candidate_id=%s ORDER BY created_at,id",
            (candidate_id,),
        ).fetchall()]
    return int(approvals), (deployment["status"] if deployment else None), stable, outcomes


def _pending_behavior(runtime):
    import json
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from app.evolution import EvolutionCandidateGenerator
    from app.learning_eval import EvaluationResult
    from tests.test_m5_controlled_evolution import Proposer, evidence
    from tests.test_m5_release_gates import cases, judge, runner

    items = evidence(runtime, tag="learning-behavior")
    suite = cases()
    runtime.evolution.freeze_research_suite(suite)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    root = runtime.costs.create_root_budget(
        "local-user", "learning", "behavior-release", max_attempts=1,
        deadline_at=deadline, limit_microusd=50_000,
    )
    batch = runtime.evolution.authorize_generation_batch(
        experience_ids=[item["id"] for item in items],
        base_bundle_id=runtime.behavior.active("stable").id,
        problem_fingerprint="missing-evidence-boundary", root_budget_id=root["id"],
        max_calls=1, budget_microusd=50_000, deadline_at=deadline,
        generation_config={"model": "configured", "temperature": 0, "network_retries": 0},
        idempotency_key="learning-behavior-batch",
    )
    candidate = EvolutionCandidateGenerator(runtime.evolution, runtime.behavior, Proposer()).run_batch(batch["id"])
    runtime.evolution.evaluate_research_replay(
        candidate["id"], expected_version=0, cases=suite, runner=runner, judge=judge,
        idempotency_key="learning-behavior-eval")
    stored_candidate = runtime.evolution.get_candidate(candidate["id"])
    assert stored_candidate["approval_eligible"] is True
    job_id = runtime.learning.enqueue("local-user", "experience", items[0]["id"], "behavior-source", "root")
    evaluation = EvaluationResult(
        target="BEHAVIOR", candidate_id=candidate["id"], rule_pass=True,
        rule_checks={"no_permission_expansion": True},
        judge={"wins": 3, "losses": 1, "ties": 0}, outcome="PASS", reason="",
        metrics={"safety_regressions": 0},
    )
    checkpoint = {
        "outcome": "PENDING_APPROVAL",
        "candidate": {
            "target": "BEHAVIOR", "candidate_id": candidate["id"], "subtype": "prompt",
            "base_bundle_id": stored_candidate["base_bundle_id"],
            "target_bundle_id": stored_candidate["target_bundle_id"],
        },
        "evaluation": evaluation.to_dict(),
    }
    with runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE learning_jobs SET status='NO_CHANGE', checkpoint_json=%s WHERE id=%s",
            (json.dumps(checkpoint), job_id),
        )
    pipeline = build_pipeline(
        runtime, mode=MODE_ACTIVE,
        decisions=StubDecisions(runtime.db, promoted_decision()),
        agent=StubAgent(lambda evidence_ids, source_refs: None),
    )
    pipeline.gate.policy = replace(pipeline.gate.policy, canary_budget_microusd=1000)
    stored = next(item for item in runtime.learning.history() if item["id"] == job_id)
    stable = runtime.behavior.active("stable").id
    return pipeline, stored, stored_candidate, items[0]["id"], stable


def _assert_behavior_not_started(runtime, candidate_id, stable_before) -> None:
    approvals, deployment, stable, outcomes = _behavior_state(runtime, candidate_id)
    assert approvals == 0
    assert deployment is None
    assert stable == stable_before
    assert "CANARY_STARTED" not in outcomes
    assert "PROMOTED" not in outcomes


def _ready_behavior_exposures(runtime, candidate_id) -> None:
    with runtime.db.transaction() as connection:
        deployment = connection.execute(
            "SELECT * FROM canary_deployments WHERE candidate_id=%s AND status='ACTIVE'",
            (candidate_id,),
        ).fetchone()
        for index in range(40):
            cohort = "champion" if index < 20 else "challenger"
            bundle = deployment["champion_bundle_id"] if index < 20 else deployment["challenger_bundle_id"]
            quality = "quality_fail" if cohort == "champion" else "quality_pass"
            connection.execute(
                "INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,"
                "request_digest,idempotency_key,exposed_at,finished_at,prompt_hit,cost_microusd,quality_outcome) "
                "VALUES (%s,%s,%s,%s,%s,1,1,%s,%s,%s,%s,1,1,%s)",
                (deployment["id"], f"offline-{index}", str(index), cohort, bundle, str(index), f"offline-{index}",
                 "2026-10-18T00:00:00+00:00", "2026-10-18T00:00:00+00:00", quality),
            )


def test_behavior_resume_approval_starts_canary_and_later_promotes_stable(v3_runtime):
    pipeline, stored, candidate, _experience_id, stable_before = _pending_behavior(v3_runtime)
    result = pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert result.outcome == "CANARY_STARTED", result.reason
    approvals, deployment, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
    assert approvals == 1 and deployment == "ACTIVE" and stable == stable_before
    assert outcomes == ["CANARY_STARTED"]
    _ready_behavior_exposures(v3_runtime, candidate["id"])
    advanced = pipeline.advance_canaries(stored["owner_id"])
    assert [item["outcome"] for item in advanced] == ["PROMOTED"]
    approvals, deployment, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
    assert approvals == 1 and deployment == "PROMOTED"
    assert stable == candidate["target_bundle_id"]
    assert outcomes == ["CANARY_STARTED", "PROMOTED"]


def test_expired_behavior_lease_before_resume_adoption_does_not_approve(v3_runtime):
    from app.learning import LearningConflict

    pipeline, stored, candidate, _experience_id, stable_before = _pending_behavior(v3_runtime)
    original = pipeline.gate.evolution.approve_current

    def expire_then_approve(*args, **kwargs):
        _expire(v3_runtime, stored["id"])
        return original(*args, **kwargs)

    pipeline.gate.evolution.approve_current = expire_then_approve
    with pytest.raises(LearningConflict, match="lease lost"):
        pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    _assert_behavior_not_started(v3_runtime, candidate["id"], stable_before)
    job = next(item for item in v3_runtime.learning.history() if item["id"] == stored["id"])
    assert job["status"] == "RUNNING"


def test_paused_policy_before_behavior_resume_adoption_does_not_approve(v3_runtime):
    from app.learning import LearningConflict

    pipeline, stored, candidate, _experience_id, stable_before = _pending_behavior(v3_runtime)
    original = pipeline.gate.evolution.approve_current

    def pause_then_approve(*args, **kwargs):
        v3_runtime.learning.configure(
            "local-user", expected_version=1, paused=True, allowed_assets=["memory", "skill", "prompt"],
        )
        return original(*args, **kwargs)

    pipeline.gate.evolution.approve_current = pause_then_approve
    with pytest.raises(LearningConflict, match="paused or changed"):
        pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    _assert_behavior_not_started(v3_runtime, candidate["id"], stable_before)
    job = next(item for item in v3_runtime.learning.history() if item["id"] == stored["id"])
    assert job["status"] == "RUNNING"


def test_revoked_behavior_source_before_resume_adoption_does_not_approve(v3_runtime):
    pipeline, stored, candidate, experience_id, stable_before = _pending_behavior(v3_runtime)
    original = pipeline.gate.evolution.approve_current

    def revoke_then_approve(*args, **kwargs):
        with v3_runtime.db.transaction() as connection:
            connection.execute(
                "UPDATE evolution_experiences SET source_state='DELETED' WHERE id=%s", (experience_id,),
            )
        return original(*args, **kwargs)

    pipeline.gate.evolution.approve_current = revoke_then_approve
    result = pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert result.outcome == "REJECTED"
    assert "deleted or revoked" in result.reason
    _assert_behavior_not_started(v3_runtime, candidate["id"], stable_before)
    job = next(item for item in v3_runtime.learning.history() if item["id"] == stored["id"])
    assert job["status"] == "REJECTED"


def _secondary_behavior_source(candidate, primary_id: str) -> str:
    others = [item for item in candidate["experience_ids"] if item != primary_id]
    assert others
    return others[0]


def test_revoked_secondary_behavior_evidence_before_resume_does_not_approve(v3_runtime):
    pipeline, stored, candidate, primary_id, stable_before = _pending_behavior(v3_runtime)
    secondary = _secondary_behavior_source(candidate, primary_id)
    original = pipeline.gate.evolution.approve_current

    def revoke_then_approve(*args, **kwargs):
        with v3_runtime.db.transaction() as connection:
            connection.execute(
                "UPDATE evolution_experiences SET source_state='DELETED' WHERE id=%s", (secondary,),
            )
        return original(*args, **kwargs)

    pipeline.gate.evolution.approve_current = revoke_then_approve
    result = pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert result.outcome == "REJECTED"
    assert "deleted or revoked" in result.reason
    _assert_behavior_not_started(v3_runtime, candidate["id"], stable_before)
    with v3_runtime.db.connection() as connection:
        state = connection.execute(
            "SELECT source_state FROM evolution_experiences WHERE id=%s", (secondary,),
        ).fetchone()["source_state"]
    assert state == "DELETED"


def test_revoked_secondary_behavior_evidence_before_release_does_not_promote(v3_runtime):
    pipeline, stored, candidate, primary_id, stable_before = _pending_behavior(v3_runtime)
    secondary = _secondary_behavior_source(candidate, primary_id)
    started = pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert started.outcome == "CANARY_STARTED", started.reason
    _ready_behavior_exposures(v3_runtime, candidate["id"])
    with v3_runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE evolution_experiences SET source_state='DELETED' WHERE id=%s", (secondary,),
        )
    advanced = pipeline.advance_canaries(stored["owner_id"])
    assert [item["outcome"] for item in advanced] == ["REJECTED"]
    _approvals, deployment, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
    assert deployment == "ACTIVE" and stable == stable_before
    assert outcomes == ["CANARY_STARTED", "REJECTED"]
    assert "PROMOTED" not in outcomes


def test_rejected_release_still_rolls_back_a_later_unsafe_canary(v3_runtime):
    pipeline, stored, candidate, primary_id, stable_before = _pending_behavior(v3_runtime)
    secondary = _secondary_behavior_source(candidate, primary_id)
    started = pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert started.outcome == "CANARY_STARTED", started.reason
    _ready_behavior_exposures(v3_runtime, candidate["id"])
    with v3_runtime.db.transaction() as connection:
        connection.execute(
            "UPDATE evolution_experiences SET source_state='DELETED' WHERE id=%s", (secondary,),
        )
    refused = pipeline.advance_canaries(stored["owner_id"])
    assert [item["outcome"] for item in refused] == ["REJECTED"]
    _approvals, deployment, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
    assert deployment == "ACTIVE" and stable == stable_before
    assert outcomes == ["CANARY_STARTED", "REJECTED"]
    assert pipeline._canary_candidates(stored["owner_id"]) == [
        {"candidate_id": candidate["id"], "target": "BEHAVIOR"},
    ]
    with v3_runtime.db.transaction() as connection:
        deployment_row = connection.execute(
            "SELECT id, champion_bundle_id FROM canary_deployments WHERE candidate_id=%s AND status='ACTIVE'",
            (candidate["id"],),
        ).fetchone()
        connection.execute(
            "INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,"
            "request_digest,idempotency_key,exposed_at,finished_at) "
            "VALUES (%s,'unsafe-after-refusal','unsafe','challenger',%s,0,0,'d','unsafe-after-refusal',"
            "'2026-10-18T00:00:00+00:00','2026-10-18T00:00:00+00:00')",
            (deployment_row["id"], deployment_row["champion_bundle_id"]),
        )
    rolled = pipeline.advance_canaries(stored["owner_id"])
    assert [item["outcome"] for item in rolled] == ["ROLLED_BACK"]
    again = pipeline.advance_canaries(stored["owner_id"])
    assert again == []
    _approvals, deployment, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
    assert deployment == "ROLLED_BACK" and stable == stable_before
    assert "PROMOTED" not in outcomes
    assert outcomes.count("ROLLED_BACK") == 1
    assert outcomes == ["CANARY_STARTED", "REJECTED", "ROLLED_BACK"]


def test_release_source_lock_orders_secondary_revoke_after_promote(v3_runtime, migrated_postgres_url):
    import threading

    import psycopg

    pipeline, stored, candidate, primary_id, stable_before = _pending_behavior(v3_runtime)
    secondary = _secondary_behavior_source(candidate, primary_id)
    started = pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert started.outcome == "CANARY_STARTED", started.reason
    _ready_behavior_exposures(v3_runtime, candidate["id"])
    locked = threading.Event()
    release = threading.Event()
    calls = {"n": 0}
    original = pipeline.gate.evolution._validate_candidate_sources
    box: dict[str, object] = {}

    def hooked(item, connection=None):
        outcome = original(item, connection=connection)
        if connection is not None and calls["n"] == 0:
            calls["n"] = 1
            locked.set()
            assert release.wait(30)
        return outcome

    def advance() -> None:
        try:
            box["result"] = pipeline.advance_canaries(stored["owner_id"])
        except Exception as exc:
            box["error"] = exc

    def revoke() -> None:
        connection = psycopg.connect(migrated_postgres_url)
        try:
            connection.execute("SET application_name = 'learning-evidence-revoker'")
            connection.execute(
                "UPDATE evolution_experiences SET source_state = 'DELETED' WHERE id = %s", (secondary,),
            )
            connection.commit()
            box["revoked"] = True
        except Exception as exc:
            box["revoke_error"] = exc
        finally:
            connection.close()

    pipeline.gate.evolution._validate_candidate_sources = hooked
    worker = threading.Thread(target=advance)
    revoker = threading.Thread(target=revoke)
    worker.start()
    try:
        assert locked.wait(30)
        revoker.start()
        assert _evidence_revoke_wait(migrated_postgres_url)
        _approvals, deployment, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
        assert deployment == "ACTIVE" and stable == stable_before
        assert outcomes == ["CANARY_STARTED"]
    finally:
        release.set()
        worker.join(40)
        revoker.join(40)
    assert box.get("error") is None and box.get("revoke_error") is None
    assert [item["outcome"] for item in box["result"]] == ["PROMOTED"]
    assert box.get("revoked") is True
    _approvals, deployment, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
    assert deployment == "PROMOTED" and stable == candidate["target_bundle_id"]
    assert outcomes == ["CANARY_STARTED", "PROMOTED"]
    with v3_runtime.db.connection() as connection:
        state = connection.execute(
            "SELECT source_state FROM evolution_experiences WHERE id=%s", (secondary,),
        ).fetchone()["source_state"]
    assert state == "DELETED"


def test_expired_lease_during_behavior_approval_lock_wait_does_not_approve(v3_runtime, migrated_postgres_url):
    import threading

    import psycopg
    from app.learning import LearningConflict

    pipeline, stored, candidate, _experience_id, stable_before = _pending_behavior(v3_runtime)
    held = threading.Event()
    release = threading.Event()
    box: dict[str, object] = {}

    def resume() -> None:
        try:
            box["result"] = pipeline.resume(
                stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True,
            )
        except Exception as exc:
            box["error"] = exc

    def hold() -> None:
        connection = psycopg.connect(migrated_postgres_url)
        try:
            connection.execute("SET application_name = 'learning-behavior-holder'")
            connection.execute("SELECT id FROM evolution_candidates WHERE id = %s FOR UPDATE", (candidate["id"],))
            held.set()
            assert release.wait(30)
        finally:
            connection.rollback()
            connection.close()

    holder = threading.Thread(target=hold)
    worker = threading.Thread(target=resume)
    holder.start()
    try:
        assert held.wait(30)
        worker.start()
        assert _behavior_lock_wait(migrated_postgres_url)
        _expire(v3_runtime, stored["id"])
    finally:
        release.set()
        holder.join(40)
        worker.join(40)
    assert isinstance(box.get("error"), LearningConflict)
    assert "lease lost" in str(box["error"])
    _assert_behavior_not_started(v3_runtime, candidate["id"], stable_before)


def test_policy_pause_during_stable_release_lock_wait_does_not_switch_stable(v3_runtime, migrated_postgres_url):
    import threading

    import psycopg

    pipeline, stored, candidate, _experience_id, stable_before = _pending_behavior(v3_runtime)
    started = pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert started.outcome == "CANARY_STARTED", started.reason
    _ready_behavior_exposures(v3_runtime, candidate["id"])
    held = threading.Event()
    release = threading.Event()
    box: dict[str, object] = {}

    def advance() -> None:
        try:
            box["result"] = pipeline.advance_canaries(stored["owner_id"])
        except Exception as exc:
            box["error"] = exc

    def hold() -> None:
        connection = psycopg.connect(migrated_postgres_url)
        try:
            connection.execute("SET application_name = 'learning-behavior-holder'")
            connection.execute("SELECT id FROM evolution_candidates WHERE id = %s FOR UPDATE", (candidate["id"],))
            held.set()
            assert release.wait(30)
        finally:
            connection.rollback()
            connection.close()

    holder = threading.Thread(target=hold)
    worker = threading.Thread(target=advance)
    holder.start()
    try:
        assert held.wait(30)
        worker.start()
        assert _behavior_lock_wait(migrated_postgres_url)
        v3_runtime.learning.configure(
            "local-user", expected_version=1, paused=True, allowed_assets=["memory", "skill", "prompt"],
        )
    finally:
        release.set()
        holder.join(40)
        worker.join(40)
    assert box.get("error") is None
    assert box.get("result") == []
    _approvals, deployment, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
    assert deployment == "ACTIVE" and stable == stable_before
    assert outcomes == ["CANARY_STARTED"]


def test_paused_learning_policy_still_rolls_back_an_unsafe_canary(v3_runtime):
    pipeline, stored, candidate, _experience_id, stable_before = _pending_behavior(v3_runtime)
    started = pipeline.resume(stored["id"], owner_id=stored["owner_id"], expected_version=stored["version"], approve=True)
    assert started.outcome == "CANARY_STARTED", started.reason
    with v3_runtime.db.transaction() as connection:
        deployment = connection.execute(
            "SELECT id, champion_bundle_id FROM canary_deployments WHERE candidate_id=%s",
            (candidate["id"],),
        ).fetchone()
        connection.execute(
            "INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,"
            "request_digest,idempotency_key,exposed_at,finished_at) "
            "VALUES (%s,'unsafe','unsafe','challenger',%s,0,0,'d','unsafe','2026-10-18T00:00:00+00:00','2026-10-18T00:00:00+00:00')",
            (deployment["id"], deployment["champion_bundle_id"]),
        )
    v3_runtime.learning.configure(
        "local-user", expected_version=1, paused=True, allowed_assets=["memory", "skill", "prompt"],
    )
    advanced = pipeline.advance_canaries(stored["owner_id"])
    assert [item["outcome"] for item in advanced] == ["ROLLED_BACK"]
    _approvals, deployment_status, stable, outcomes = _behavior_state(v3_runtime, candidate["id"])
    assert deployment_status == "ROLLED_BACK"
    assert stable == stable_before
    assert "PROMOTED" not in outcomes
    assert "ROLLED_BACK" in outcomes


def _evidence_revoke_wait(url: str) -> bool:
    import time

    import psycopg

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with psycopg.connect(url) as connection:
            waiting = connection.execute(
                """
                SELECT 1 FROM pg_stat_activity
                WHERE datname = current_database() AND state = 'active' AND wait_event_type = 'Lock'
                  AND application_name = 'learning-evidence-revoker'
                LIMIT 1
                """
            ).fetchone()
        if waiting is not None:
            return True
        time.sleep(0.05)
    return False


def _behavior_lock_wait(url: str) -> bool:
    import time

    import psycopg

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with psycopg.connect(url) as connection:
            waiting = connection.execute(
                """
                SELECT 1 FROM pg_stat_activity
                WHERE datname = current_database() AND state = 'active' AND wait_event_type = 'Lock'
                  AND query LIKE '%evolution_candidates%' AND query LIKE '%FOR UPDATE%'
                  AND application_name <> 'learning-behavior-holder'
                LIMIT 1
                """
            ).fetchone()
        if waiting is not None:
            return True
        time.sleep(0.05)
    return False
