from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Barrier
import json
import uuid

import pytest


def _seed_turn(connection, thread_id: str, turn_id: str, created_at: str) -> None:
    connection.execute(
        "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
        "VALUES (%s,%s,%s,'ACCEPTED',%s,%s)",
        (turn_id, thread_id, f"client-{turn_id}", created_at, created_at),
    )
    connection.execute(
        "INSERT INTO turn_jobs(turn_id,thread_id,status) VALUES (%s,%s,'QUEUED')",
        (turn_id, thread_id),
    )


def _seed_queue(db, *, thread_count: int, turns_per_thread: int = 1) -> list[list[str]]:
    prefix = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    turn_ids: list[list[str]] = []
    with db.transaction() as connection:
        for thread_index in range(thread_count):
            thread_id = f"queue-thread-{prefix}-{thread_index}"
            connection.execute(
                "INSERT INTO threads(id,title,created_at,updated_at) VALUES (%s,%s,%s,%s)",
                (thread_id, "queue", now, now),
            )
            thread_turns = []
            for turn_index in range(turns_per_thread):
                turn_id = f"queue-turn-{prefix}-{thread_index}-{turn_index}"
                _seed_turn(connection, thread_id, turn_id, now)
                thread_turns.append(turn_id)
            turn_ids.append(thread_turns)
    return turn_ids


def _clear_turn_jobs(db) -> None:
    with db.transaction() as connection:
        connection.execute("DELETE FROM turn_jobs")


def test_durable_queue_concurrent_claims_are_unique_per_thread(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.durable_queue import DurableQueue

    db = Database(migrated_postgres_url, workspace=tmp_path / "concurrent-workspace")
    _clear_turn_jobs(db)
    turns = _seed_queue(db, thread_count=2, turns_per_thread=2)
    start = Barrier(4)

    def claim(index: int):
        start.wait(timeout=5)
        return DurableQueue(db).claim_next(f"concurrent-worker-{index}", 30)

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            tokens = [
                token
                for token in executor.map(claim, range(4))
                if token is not None
            ]
        assert len(tokens) == 2
        assert len({token.job_id for token in tokens}) == 2
        assert len({token.thread_id for token in tokens}) == 2
        assert {token.job_id for token in tokens}.issubset(
            {turn_id for per_thread in turns for turn_id in per_thread}
        )
    finally:
        db.close()


def test_durable_queue_enforces_global_running_limit_across_connections(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.durable_queue import DurableQueue

    db = Database(migrated_postgres_url, workspace=tmp_path / "capacity-workspace")
    _clear_turn_jobs(db)
    _seed_queue(db, thread_count=4)
    start = Barrier(4)

    def claim(index: int):
        start.wait(timeout=5)
        return DurableQueue(db, max_running=2).claim_next(f"capacity-worker-{index}", 30)

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            tokens = [token for token in executor.map(claim, range(4)) if token is not None]
        assert len(tokens) == 2
        with db.connection() as connection:
            running = connection.execute(
                "SELECT COUNT(*) FROM turn_jobs WHERE status='RUNNING' "
                "AND lease_until>clock_timestamp()"
            ).fetchone()[0]
        assert running == 2
    finally:
        db.close()


def test_durable_queue_request_cancel_is_idempotent_and_cancels_queued_job(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.durable_queue import DurableQueue

    db = Database(migrated_postgres_url, workspace=tmp_path / "cancel-workspace")
    _clear_turn_jobs(db)
    turn_id = _seed_queue(db, thread_count=1)[0][0]
    queue = DurableQueue(db)
    try:
        assert queue.request_cancel(turn_id) is True
        assert queue.request_cancel(turn_id) is False
        assert queue.claim_next("cancel-worker", 30) is None
        with db.connection() as connection:
            row = connection.execute(
                "SELECT status,cancel_requested_at,finished_at FROM turn_jobs WHERE turn_id=%s",
                (turn_id,),
            ).fetchone()
        assert row["status"] == "CANCELLED"
        assert row["cancel_requested_at"] is not None
        assert row["finished_at"] is not None
    finally:
        db.close()


def test_durable_queue_fail_and_heartbeat_are_epoch_fenced(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.durable_queue import DurableQueue, LeaseLost

    db = Database(migrated_postgres_url, workspace=tmp_path / "fence-workspace")
    _clear_turn_jobs(db)
    _seed_queue(db, thread_count=1)
    queue = DurableQueue(db)
    first = queue.claim_next("same-owner", 30)
    assert first is not None
    try:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE turn_jobs SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE turn_id=%s",
                (first.job_id,),
            )
        replacement = queue.claim_next("same-owner", 30)
        assert replacement is not None and replacement.epoch > first.epoch
        with pytest.raises(LeaseLost):
            queue.heartbeat(first, 30)
        with pytest.raises(LeaseLost):
            queue.fail(first, 'late "failure"')
        queue.heartbeat(replacement, 30)
        queue.fail(replacement, 'provider "timeout"')
        with db.connection() as connection:
            row = connection.execute(
                "SELECT status,last_error_json FROM turn_jobs WHERE turn_id=%s",
                (replacement.job_id,),
            ).fetchone()
        assert row["status"] == "FAILED"
        assert json.loads(row["last_error_json"]) == {"error_code": 'provider "timeout"'}
    finally:
        db.close()


def test_durable_queue_recover_expired_invalidates_epoch_before_reclaim(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.durable_queue import DurableQueue, LeaseLost

    db = Database(migrated_postgres_url, workspace=tmp_path / "recovery-workspace")
    _clear_turn_jobs(db)
    _seed_queue(db, thread_count=1)
    queue = DurableQueue(db)
    token = queue.claim_next("crashed-worker", 30)
    assert token is not None
    try:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE turn_jobs SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE turn_id=%s",
                (token.job_id,),
            )
        assert queue.recover_expired() == 1
        with db.connection() as connection:
            recovered = connection.execute(
                "SELECT status,lease_owner,lease_until,lease_epoch FROM turn_jobs WHERE turn_id=%s",
                (token.job_id,),
            ).fetchone()
        assert recovered["status"] == "QUEUED"
        assert recovered["lease_owner"] is None
        assert recovered["lease_until"] is None
        assert recovered["lease_epoch"] == token.epoch + 1
        with pytest.raises(LeaseLost):
            queue.heartbeat(token, 30)
        replacement = queue.claim_next("recovery-worker", 30)
        assert replacement is not None
        assert replacement.epoch == token.epoch + 2
    finally:
        db.close()
def test_durable_queue_serializes_each_thread_and_fences_old_epoch(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.durable_queue import DurableQueue, LeaseLost

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    _clear_turn_jobs(db)
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,created_at,updated_at) VALUES ('queue-thread','queue',%s,%s)",
            (now, now),
        )
        _seed_turn(connection, "queue-thread", "queue-turn-1", now)
        _seed_turn(connection, "queue-thread", "queue-turn-2", now)

    first = DurableQueue(db).claim_next("worker-a", 30)
    assert first is not None
    assert DurableQueue(db).claim_next("worker-b", 30) is None
    DurableQueue(db).finish(first)
    second = DurableQueue(db).claim_next("worker-b", 30)
    assert second is not None and second.job_id == "queue-turn-2"

    with db.transaction() as connection:
        connection.execute(
            "UPDATE turn_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE turn_id=%s",
            (second.job_id,),
        )
    replacement = DurableQueue(db).claim_next("worker-c", 30)
    assert replacement is not None and replacement.epoch > second.epoch
    try:
        DurableQueue(db).finish(second)
    except LeaseLost:
        pass
    else:
        raise AssertionError("stale lease epoch completed a job")
    DurableQueue(db).finish(replacement)
    db.close()


@dataclass
class FixedEmbeddingProvider:
    dimensions: int = 1024
    model: str = "Qwen/Qwen3-Embedding-4B"
    first_value: float = 1.0

    def embed(self, texts):
        from app.embedding import EmbeddingBatch

        vectors = tuple((self.first_value, *([0.0] * (self.dimensions - 1))) for _ in texts)
        return EmbeddingBatch(vectors, self.model, self.dimensions)


def test_memory_outbox_worker_and_hnsw_semantic_retrieval(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker
    from app.memory_v2 import MemoryContextProvider, MemoryContextRequest, MemoryStore

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,created_at,updated_at) VALUES ('memory-thread','memory',%s,%s)",
            (now, now),
        )
    store = MemoryStore(db, tmp_path / "memory")
    memory = store.remember(
        "local-user", "preference", "user", "", "Prefer concise answers",
        "integration-memory-1", pinned=True,
    )
    with db.connection() as connection:
        queued = connection.execute(
            "SELECT status FROM embedding_jobs WHERE revision_id=%s", (memory.revision_id,)
        ).fetchone()
        hnsw = connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname='memory_embeddings_hnsw_cos_idx'"
        ).fetchone()
    assert queued["status"] == "QUEUED"
    assert "USING hnsw" in hnsw["indexdef"]

    assert EmbeddingWorker(db, FixedEmbeddingProvider(), owner="embedding-test").run_once()
    with db.connection() as connection:
        job = connection.execute(
            "SELECT status,last_error_code,last_error_json FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
        vector_count = connection.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE revision_id=%s", (memory.revision_id,)
        ).fetchone()[0]
    assert job["status"] == "COMPLETED", job
    assert vector_count == 1
    provider = MemoryContextProvider(db, embedding_provider=FixedEmbeddingProvider())
    bundle = provider.select(
        MemoryContextRequest("local-user", "memory-thread", None, "Prefer concise answers")
    )
    assert memory.revision_id in bundle.revision_ids
    assert bundle.trace["retrieval_mode"] == "hybrid", bundle.trace
    assert bundle.trace["semantic_candidate_count"] == 1
    assert bundle.trace["lexical_candidate_count"] == 1
    db.close()


@pytest.mark.parametrize(
    "query_provider",
    [
        FixedEmbeddingProvider(model="different-embedding-space"),
        FixedEmbeddingProvider(dimensions=512),
    ],
)
def test_memory_query_embedding_must_match_active_profile(
    migrated_postgres_url, tmp_path, query_provider
) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker
    from app.memory_v2 import MemoryContextProvider, MemoryContextRequest, MemoryStore

    db = Database(migrated_postgres_url, workspace=tmp_path / "profile-workspace")
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,created_at,updated_at) VALUES ('profile-thread','memory',%s,%s)",
            (now, now),
        )
    memory = MemoryStore(db, tmp_path / "memory").remember(
        "local-user", "preference", "user", "", "Prefer concise answers",
        "profile-memory-1",
    )
    assert EmbeddingWorker(db, FixedEmbeddingProvider(), owner="embedding-test").run_once()

    bundle = MemoryContextProvider(
        db,
        embedding_provider=query_provider,
    ).select(MemoryContextRequest("local-user", "profile-thread", None, "Prefer concise answers"))

    assert memory.revision_id in bundle.revision_ids
    assert bundle.trace["retrieval_mode"] == "lexical_fallback"
    assert bundle.trace["fallback_reason"] == "profile_mismatch"
    assert bundle.trace["semantic_candidate_count"] == 0
    assert bundle.trace["lexical_candidate_count"] == 1
    db.close()


def test_low_quality_semantic_candidates_fall_back_to_lexical_search(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker
    from app.memory_v2 import MemoryContextProvider, MemoryContextRequest, MemoryStore

    db = Database(migrated_postgres_url, workspace=tmp_path / "quality-workspace")
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,created_at,updated_at) VALUES ('quality-thread','memory',%s,%s)",
            (now, now),
        )
    memory = MemoryStore(db, tmp_path / "memory").remember(
        "local-user", "preference", "user", "", "Prefer concise answers",
        "quality-memory-1",
    )
    assert EmbeddingWorker(db, FixedEmbeddingProvider(), owner="embedding-test").run_once()

    bundle = MemoryContextProvider(
        db,
        embedding_provider=FixedEmbeddingProvider(first_value=-1.0),
    ).select(MemoryContextRequest("local-user", "quality-thread", None, "Prefer concise answers"))

    assert memory.revision_id in bundle.revision_ids
    assert bundle.trace["retrieval_mode"] == "lexical_fallback"
    assert bundle.trace["fallback_reason"] == "semantic_quality_below_threshold"
    assert bundle.trace["semantic_candidate_count"] == 0
    assert bundle.trace["lexical_candidate_count"] == 1
    db.close()


def test_partial_embedding_coverage_combines_semantic_and_lexical_search(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker
    from app.memory_v2 import MemoryContextProvider, MemoryContextRequest, MemoryStore

    db = Database(migrated_postgres_url, workspace=tmp_path / "coverage-workspace")
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,created_at,updated_at) VALUES ('coverage-thread','memory',%s,%s)",
            (now, now),
        )
    store = MemoryStore(db, tmp_path / "memory")
    semantic = store.remember(
        "local-user", "preference", "user", "", "Cycling cadence target",
        "coverage-memory-1",
    )
    expected = store.remember(
        "local-user", "preference", "user", "", "Prefer concise answers",
        "coverage-memory-2",
    )
    assert EmbeddingWorker(db, FixedEmbeddingProvider(), owner="embedding-test").run_once()

    bundle = MemoryContextProvider(db, embedding_provider=FixedEmbeddingProvider()).select(
        MemoryContextRequest("local-user", "coverage-thread", None, "Prefer concise answers")
    )

    assert expected.revision_id in bundle.revision_ids
    assert semantic.revision_id in bundle.revision_ids
    assert len(bundle.revision_ids) == 2
    assert bundle.trace["retrieval_mode"] == "hybrid"
    assert bundle.trace["fallback_reason"] == ""
    assert bundle.trace["embedding_coverage"] == pytest.approx(0.5)
    assert bundle.trace["semantic_candidate_count"] == 1
    assert bundle.trace["lexical_candidate_count"] == 1
    db.close()


def test_archive_concurrent_retry_only_requeues_once(migrated_postgres_url, tmp_path):
    import asyncio
    from app.db import Database
    from app.memory_archive import ArchiveError, ConversationArchiver
    from app.memory_v2 import MemoryStore
    from test_memory_archive import valid_summary

    db = Database(migrated_postgres_url, workspace=tmp_path / "retry-workspace")
    now = datetime.now(timezone.utc).isoformat()
    with db.transaction() as c:
        c.execute("INSERT INTO threads(id,title,created_at,updated_at) VALUES ('retry-thread','retry',?,?)", (now, now))
        c.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES ('retry-turn','retry-thread','retry-turn','COMPLETED',?,?)", (now, now))
        c.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,message_seq,created_at) VALUES ('retry-message','retry-thread','retry-turn','user','original','ready',1,?)", (now,))
    async def invalid(payload): return "invalid"
    arch = ConversationArchiver(db, MemoryStore(db, tmp_path / "memory"), invalid, keep_messages=0, max_attempts=1)
    asyncio.run(arch.archive_thread("retry-thread"))
    job = arch.status("retry-thread", "local-user")["jobs"][0]
    barrier = Barrier(2)
    build = arch.transcripts.build
    def simultaneous_build(*args, **kwargs):
        result = build(*args, **kwargs)
        barrier.wait(timeout=10)
        return result
    arch.transcripts.build = simultaneous_build
    def retry():
        try:
            return arch.retry("retry-thread", "local-user", job["id"], job["updated_at"])["status"]
        except ArchiveError:
            return "CONFLICT"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: retry(), range(2)))
    arch.transcripts.build = build
    assert sorted(results) == ["CONFLICT", "QUEUED"]
    assert arch.status("retry-thread", "local-user")["archived_through_seq"] == 0
    arch.summarizer = valid_summary
    assert asyncio.run(arch.process(arch.claim(job["id"]))) is not None
    assert arch.claim(job["id"]) is None
    with db.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 1
        assert c.execute("SELECT content FROM thread_messages WHERE id='retry-message'").fetchone()[0] == "original"
    db.close()
