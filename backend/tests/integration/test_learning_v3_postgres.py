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
from app.startup import build_runtime

from tests.test_learning_pipeline_v3 import StubAgent, StubDecisions, enqueue, stub_budget
from tests.test_learning_targets import memory_draft


@pytest.fixture
def v3_runtime(tmp_path, migrated_postgres_url, monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)
    runtime = build_runtime(tmp_path, database_url=migrated_postgres_url)
    runtime.learning.configure("local-user", expected_version=0, paused=False,
                               allowed_assets=["memory", "skill", "prompt"])
    yield runtime
    runtime.db._pool.close()


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
