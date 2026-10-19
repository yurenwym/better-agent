from __future__ import annotations

import uuid

import pytest


def _memory(db, tmp_path, *, key: str):
    from app.memory_v2 import MemoryStore

    return MemoryStore(db, tmp_path / f"memory-{key}").remember(
        "local-user",
        "fact",
        "user",
        "",
        f"embedding worker test {key}",
        f"embedding-worker-{key}",
    )


class FailingProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def embed(self, _texts):
        self.calls += 1
        raise self.error


class FixedProvider:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        from app.embedding import EmbeddingBatch

        self.calls += 1
        vector = (1.0, *([0.0] * 1023))
        return EmbeddingBatch(
            tuple(vector for _ in texts), "Qwen/Qwen3-Embedding-4B", 1024
        )


def test_retryable_provider_failure_retries_then_dead_letters(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding import EmbeddingRequestError
    from app.embedding_worker import EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    memory = _memory(db, tmp_path, key=uuid.uuid4().hex)
    provider = FailingProvider(
        EmbeddingRequestError("provider unavailable", kind="server", retryable=True)
    )
    worker = EmbeddingWorker(db, provider, owner="embedding-retry-test")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE embedding_jobs SET max_attempts=2 WHERE revision_id=%s",
            (memory.revision_id,),
        )

    assert worker.run_once()
    with db.connection() as connection:
        first = connection.execute(
            "SELECT status,attempts,last_error_code,lease_owner,lease_until "
            "FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
    assert first["status"] == "RETRY_WAIT"
    assert first["attempts"] == 1
    assert first["last_error_code"] == "server"
    assert first["lease_owner"] is None and first["lease_until"] is None

    with db.transaction() as connection:
        connection.execute(
            "UPDATE embedding_jobs SET available_at=clock_timestamp() WHERE revision_id=%s",
            (memory.revision_id,),
        )
    assert worker.run_once()
    with db.connection() as connection:
        final = connection.execute(
            "SELECT status,attempts,last_error_code FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
        vectors = connection.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()[0]
    assert final["status"] == "DEAD_LETTER"
    assert final["attempts"] == 2
    assert final["last_error_code"] == "server"
    assert vectors == 0
    assert provider.calls == 2
    db.close()


def test_non_retryable_protocol_failure_dead_letters_immediately(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding import EmbeddingProtocolError
    from app.embedding_worker import EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    memory = _memory(db, tmp_path, key=uuid.uuid4().hex)
    provider = FailingProvider(EmbeddingProtocolError("wrong dimensions"))

    assert EmbeddingWorker(db, provider, owner="embedding-protocol-test").run_once()
    with db.connection() as connection:
        job = connection.execute(
            "SELECT status,attempts,last_error_code FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
    assert job["status"] == "DEAD_LETTER"
    assert job["attempts"] == 1
    assert job["last_error_code"] == "protocol"
    db.close()


def test_malformed_provider_vector_is_rejected_before_pgvector_write(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding import EmbeddingBatch
    from app.embedding_worker import EmbeddingWorker

    class MalformedProvider:
        def embed(self, _texts):
            return EmbeddingBatch(
                ((1.0, 0.0),), "Qwen/Qwen3-Embedding-4B", 1024
            )

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    memory = _memory(db, tmp_path, key=uuid.uuid4().hex)

    assert EmbeddingWorker(db, MalformedProvider(), owner="embedding-shape-test").run_once()
    with db.connection() as connection:
        job = connection.execute(
            "SELECT status,last_error_code FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
        vectors = connection.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()[0]
    assert job["status"] == "DEAD_LETTER"
    assert job["last_error_code"] == "protocol"
    assert vectors == 0
    db.close()


def test_stale_revision_job_never_activates_an_embedding(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker
    from app.memory_v2 import MemoryStore

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    key = uuid.uuid4().hex
    store = MemoryStore(db, tmp_path / f"memory-{key}")
    memory = _memory(db, tmp_path, key=key)
    provider = FixedProvider()
    worker = EmbeddingWorker(db, provider, owner="embedding-stale-test")
    lease = worker.claim()
    assert lease is not None and lease.revision_id == memory.revision_id

    updated = store.edit(
        memory.id,
        "local-user",
        "updated content is now authoritative",
        memory.revision_id,
        idempotency_key=f"embedding-worker-update-{key}",
    )
    worker.process(lease)

    with db.connection() as connection:
        stale = connection.execute(
            "SELECT status,last_error_code FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
        active_vectors = connection.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()[0]
        current = connection.execute(
            "SELECT current_revision_id FROM memory_entries WHERE id=%s", (memory.id,)
        ).fetchone()[0]
    assert current == updated.revision_id
    assert stale["status"] == "DEAD_LETTER"
    assert stale["last_error_code"] == "stale_revision"
    assert active_vectors == 0
    assert provider.calls == 0
    assert worker.run_once()
    db.close()


def test_old_epoch_cannot_heartbeat_or_publish_after_takeover(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingLeaseLost, EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    memory = _memory(db, tmp_path, key=uuid.uuid4().hex)
    first_provider = FixedProvider()
    first = EmbeddingWorker(db, first_provider, owner="embedding-worker-one")
    old_lease = first.claim(lease_seconds=30)
    assert old_lease is not None
    first.heartbeat(old_lease, lease_seconds=30)

    with db.transaction() as connection:
        connection.execute(
            "UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE revision_id=%s",
            (memory.revision_id,),
        )
    second = EmbeddingWorker(db, FixedProvider(), owner="embedding-worker-two")
    current_lease = second.claim(lease_seconds=30)
    assert current_lease is not None and current_lease.epoch > old_lease.epoch

    with pytest.raises(EmbeddingLeaseLost):
        first.heartbeat(old_lease, lease_seconds=30)
    with pytest.raises(EmbeddingLeaseLost):
        first.process(old_lease)

    with db.connection() as connection:
        job = connection.execute(
            "SELECT status,lease_owner,lease_epoch FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
        vector_count = connection.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()[0]
    assert job["status"] == "RUNNING"
    assert job["lease_owner"] == "embedding-worker-two"
    assert job["lease_epoch"] == current_lease.epoch
    assert vector_count == 0
    assert first_provider.calls == 0

    second.process(current_lease)
    db.close()


def test_expired_lease_does_not_call_the_provider_or_write(migrated_postgres_url, tmp_path) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingLeaseLost, EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    memory = _memory(db, tmp_path, key=uuid.uuid4().hex)
    provider = FixedProvider()
    worker = EmbeddingWorker(db, provider, owner="embedding-before-request")
    lease = worker.claim()
    assert lease is not None
    with db.transaction() as connection:
        connection.execute(
            "UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE revision_id=%s",
            (memory.revision_id,),
        )
    with pytest.raises(EmbeddingLeaseLost):
        worker.process(lease)
    with db.connection() as connection:
        job = connection.execute(
            "SELECT status,lease_owner,lease_epoch,last_error_code FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
        vectors = connection.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE revision_id=%s", (memory.revision_id,),
        ).fetchone()[0]
    assert provider.calls == 0
    assert job["status"] == "RUNNING"
    assert job["lease_owner"] == "embedding-before-request"
    assert job["lease_epoch"] == lease.epoch
    assert job["last_error_code"] is None
    assert vectors == 0
    with db.connection() as connection:
        invented = connection.execute(
            "SELECT COUNT(*) FROM thread_events WHERE type LIKE 'embedding.%'"
        ).fetchone()[0]
    assert invented == 0
    db.close()


def test_provider_call_is_discarded_when_the_lease_expires_before_commit(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding import EmbeddingBatch
    from app.embedding_worker import EmbeddingLeaseLost, EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    memory = _memory(db, tmp_path, key=uuid.uuid4().hex)

    class ExpireDuringCall:
        def __init__(self) -> None:
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            with db.transaction() as connection:
                connection.execute(
                    "UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE revision_id=%s",
                    (memory.revision_id,),
                )
            vector = (1.0, *([0.0] * 1023))
            return EmbeddingBatch(tuple(vector for _ in texts), "Qwen/Qwen3-Embedding-4B", 1024)

    provider = ExpireDuringCall()
    worker = EmbeddingWorker(db, provider, owner="embedding-after-request")
    lease = worker.claim()
    assert lease is not None
    with pytest.raises(EmbeddingLeaseLost):
        worker.process(lease)
    with db.connection() as connection:
        job = connection.execute(
            "SELECT status FROM embedding_jobs WHERE revision_id=%s", (memory.revision_id,),
        ).fetchone()
        vectors = connection.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE revision_id=%s", (memory.revision_id,),
        ).fetchone()[0]
        committed = connection.execute(
            "SELECT COUNT(*) FROM thread_events WHERE type='embedding.committed'"
        ).fetchone()[0]
    assert provider.calls == 1
    assert job["status"] == "RUNNING"
    assert vectors == 0
    assert committed == 0
    db.close()


def test_inactive_profile_is_rejected_before_the_provider(migrated_postgres_url, tmp_path) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    memory = _memory(db, tmp_path, key=uuid.uuid4().hex)
    provider = FixedProvider()
    worker = EmbeddingWorker(db, provider, owner="embedding-profile")
    lease = worker.claim()
    assert lease is not None
    try:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE embedding_profiles SET active=false WHERE id=%s", (lease.profile_id,),
            )
        worker.process(lease)
        with db.connection() as connection:
            job = connection.execute(
                "SELECT status,last_error_code FROM embedding_jobs WHERE revision_id=%s",
                (memory.revision_id,),
            ).fetchone()
            vectors = connection.execute(
                "SELECT COUNT(*) FROM memory_embeddings WHERE revision_id=%s", (memory.revision_id,),
            ).fetchone()[0]
        assert provider.calls == 0
        assert job["status"] == "DEAD_LETTER"
        assert job["last_error_code"] == "profile_inactive"
        assert vectors == 0
    finally:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE embedding_profiles SET active=true WHERE id=%s", (lease.profile_id,),
            )
        db.close()


def test_only_one_worker_claims_the_same_job(migrated_postgres_url, tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.db import Database
    from app.embedding_worker import EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    memory = _memory(db, tmp_path, key=uuid.uuid4().hex)
    barrier = Barrier(2)

    def claim(owner: str):
        barrier.wait()
        return EmbeddingWorker(db, FixedProvider(), owner=owner).claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(claim, ("embedding-a", "embedding-b")))
    winners = [item for item in leases if item is not None]
    assert len(winners) == 1
    with db.connection() as connection:
        job = connection.execute(
            "SELECT lease_owner,lease_epoch,status FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
    assert job["status"] == "RUNNING"
    assert job["lease_owner"] == winners[0].owner
    assert job["lease_epoch"] == winners[0].epoch
    db.close()


def test_claim_recovery_and_commit_follow_the_real_turn(migrated_postgres_url, tmp_path) -> None:
    from app.conversation import ConversationService
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker
    from app.memory_v2 import MemoryStore
    from app.trace_query import TraceQuery

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    conversation = ConversationService(db)
    thread = conversation.create_thread("embedding trace")
    conversation.accept_turn(thread.id, "one", "please remember the embedding fact", [])
    with db.connection() as connection:
        message_id = connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=%s AND role='user'",
            (thread.id,),
        ).fetchone()["id"]
    key = uuid.uuid4().hex
    memory = MemoryStore(db, tmp_path / f"memory-{key}").remember(
        "local-user", "fact", "user", "", f"embedding trace {key}", f"embedding-trace-{key}",
        [{"source_type": "thread_message", "source_id": message_id}],
        source_thread_id=thread.id,
    )
    first = EmbeddingWorker(db, FixedProvider(), owner="embedding-trace-a")
    lease = first.claim()
    assert lease is not None and lease.revision_id == memory.revision_id
    with db.transaction() as connection:
        connection.execute(
            "UPDATE embedding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE revision_id=%s",
            (memory.revision_id,),
        )
    second = EmbeddingWorker(db, FixedProvider(), owner="embedding-trace-b")
    recovered = second.claim()
    assert recovered is not None and recovered.epoch > lease.epoch
    second.process(recovered)
    view = TraceQuery(db).by_thread(thread.id, owner_id="local-user")
    kinds = [event["type"] for event in view["events"] if event["type"].startswith("embedding.")]
    assert kinds == ["embedding.claimed", "embedding.recovered", "embedding.committed"]
    assert "embedding" in view["sources"]["connected"]
    assert "embedding" not in view["sources"]["deferred"]
    committed = next(event for event in view["events"] if event["type"] == "embedding.committed")
    assert committed["source"] == "embedding"
    assert committed["outcome"]["status"] == "completed"
    with db.connection() as connection:
        turn_id = connection.execute(
            "SELECT turn_id FROM thread_messages WHERE id=%s", (message_id,),
        ).fetchone()["turn_id"]
    assert committed["context"]["context"]["turn_id"] == turn_id
    assert memory.content not in str(committed["payload"])
    recovered_event = next(event for event in view["events"] if event["type"] == "embedding.recovered")
    assert recovered_event["outcome"]["error"]["retryable"] is True
    db.close()


def test_trace_write_failure_rolls_back_the_claim(migrated_postgres_url, tmp_path, monkeypatch) -> None:
    from app.conversation import ConversationService
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker
    from app.events import ThreadEventStore
    from app.memory_v2 import MemoryStore

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    conversation = ConversationService(db)
    thread = conversation.create_thread("embedding rollback")
    conversation.accept_turn(thread.id, "one", "remember this before the claim fails", [])
    with db.connection() as connection:
        message_id = connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=%s AND role='user'",
            (thread.id,),
        ).fetchone()["id"]
    key = uuid.uuid4().hex
    memory = MemoryStore(db, tmp_path / f"memory-{key}").remember(
        "local-user", "fact", "user", "", f"embedding rollback {key}", f"embedding-rollback-{key}",
        [{"source_type": "thread_message", "source_id": message_id}],
        source_thread_id=thread.id,
    )

    def fail_append(self, *args, **kwargs):
        raise RuntimeError("event store failed")

    monkeypatch.setattr(ThreadEventStore, "append", fail_append)
    with pytest.raises(RuntimeError, match="event store failed"):
        EmbeddingWorker(db, FixedProvider(), owner="embedding-rollback").claim()
    with db.connection() as connection:
        job = connection.execute(
            "SELECT status,lease_owner,lease_epoch FROM embedding_jobs WHERE revision_id=%s",
            (memory.revision_id,),
        ).fetchone()
        events = connection.execute(
            "SELECT COUNT(*) FROM thread_events WHERE type LIKE 'embedding.%'"
        ).fetchone()[0]
    assert job["status"] == "QUEUED"
    assert job["lease_owner"] is None
    assert job["lease_epoch"] == 0
    assert events == 0
    db.close()


class SignedProvider:
    def __init__(self, first: float) -> None:
        self.calls = 0
        self.first = first

    def embed(self, texts):
        from app.embedding import EmbeddingBatch

        self.calls += 1
        vector = (self.first, *([0.0] * 1023))
        return EmbeddingBatch(tuple(vector for _ in texts), "Qwen/Qwen3-Embedding-4B", 1024)


def _linked_memory(db, tmp_path, *, key: str, title: str):
    from app.conversation import ConversationService
    from app.memory_v2 import MemoryStore

    conversation = ConversationService(db)
    thread = conversation.create_thread(title)
    conversation.accept_turn(thread.id, "one", title, [])
    with db.connection() as connection:
        message_id = connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=%s AND role='user'",
            (thread.id,),
        ).fetchone()["id"]
    return MemoryStore(db, tmp_path / f"memory-{key}").remember(
        "local-user", "fact", "user", "", f"{title} {key}", f"{title}-{key}",
        [{"source_type": "thread_message", "source_id": message_id}],
        source_thread_id=thread.id,
    )


def _vector_text(db, revision_id: str) -> str:
    with db.connection() as connection:
        return connection.execute(
            "SELECT embedding::text AS embedding FROM memory_embeddings WHERE revision_id=%s",
            (revision_id,),
        ).fetchone()["embedding"]


def _committed_events(db) -> int:
    with db.connection() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM thread_events WHERE type='embedding.committed'"
        ).fetchone()[0]


def _vector_write_waiting(url: str) -> bool:
    import psycopg

    with psycopg.connect(url) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM pg_stat_activity
            WHERE datname=current_database() AND state='active' AND wait_event_type='Lock'
              AND query LIKE '%INSERT INTO memory_embeddings%'
            LIMIT 1
            """
        ).fetchone()
    return row is not None


def _contender_blocked(url: str) -> bool:
    import psycopg

    with psycopg.connect(url) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM pg_stat_activity
            WHERE application_name='embedding-source-contender' AND wait_event_type='Lock'
            LIMIT 1
            """
        ).fetchone()
    return row is not None


def _race_commit(url: str, lease, worker, contender) -> str:
    """Block the vector upsert, then run a source change beside that wait.

    Returns ``committed-during-wait`` when the source change commits while the
    upsert is waiting, or ``blocked-by-commit-lock`` when that change cannot
    commit until the worker releases the entry or profile lock.
    """
    import time
    from threading import Event, Thread

    ready, release = Event(), Event()
    state: dict[str, str] = {}

    def hold_vector():
        import psycopg

        with psycopg.connect(url, autocommit=False) as connection:
            connection.execute(
                "SELECT 1 FROM memory_embeddings WHERE revision_id=%s AND profile_id=%s FOR UPDATE",
                (lease.revision_id, lease.profile_id),
            )
            ready.set()
            if not release.wait(20):
                raise TimeoutError("vector lock was not released")
            connection.rollback()

    def run_worker():
        try:
            worker.process(lease)
        except Exception as exc:
            state["worker_error"] = type(exc).__name__

    def run_contender():
        try:
            contender()
        except Exception as exc:
            state["contender"] = f"error:{type(exc).__name__}: {exc}"
            return
        state["contender"] = "committed"

    holder, runner, rival = Thread(target=hold_vector), Thread(target=run_worker), Thread(target=run_contender)
    holder.start()
    verdict = ""
    try:
        assert ready.wait(10), "vector row was not locked"
        runner.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _vector_write_waiting(url):
            if not runner.is_alive():
                break
            time.sleep(0.05)
        assert _vector_write_waiting(url), f"vector write did not wait; state={state}"
        rival.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not verdict:
            if state.get("contender") == "committed":
                verdict = "committed-during-wait"
                break
            if state.get("contender", "").startswith("error:"):
                break
            if _contender_blocked(url):
                verdict = "blocked-by-commit-lock"
                break
            time.sleep(0.05)
    finally:
        release.set()
        if runner.ident is not None:
            runner.join(20)
        if rival.ident is not None:
            rival.join(20)
        holder.join(10)
    assert not state.get("contender", "").startswith("error:"), state.get("contender")
    assert verdict, "source change neither committed nor blocked while the vector write waited"
    assert not runner.is_alive() and not holder.is_alive()
    return verdict


def test_profile_deactivation_cannot_land_under_a_waiting_vector_write(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    key = uuid.uuid4().hex
    memory = _linked_memory(db, tmp_path, key=key, title="profile race")
    EmbeddingWorker(db, FixedProvider(), owner="embedding-profile-seed").run_once()
    before = _vector_text(db, memory.revision_id)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE embedding_jobs SET status='QUEUED',lease_owner=NULL,lease_until=NULL,"
            "available_at=clock_timestamp() WHERE revision_id=%s",
            (memory.revision_id,),
        )
    worker = EmbeddingWorker(db, SignedProvider(-1.0), owner="embedding-profile-race")
    lease = worker.claim()
    assert lease is not None
    events_before = _committed_events(db)

    def deactivate():
        import psycopg

        with psycopg.connect(migrated_postgres_url, autocommit=False) as connection:
            connection.execute("SET application_name = 'embedding-source-contender'")
            connection.execute(
                "UPDATE embedding_profiles SET active=false WHERE id=%s", (lease.profile_id,),
            )
            connection.commit()

    try:
        verdict = _race_commit(migrated_postgres_url, lease, worker, deactivate)
        with db.connection() as connection:
            job = connection.execute(
                "SELECT status FROM embedding_jobs WHERE revision_id=%s AND profile_id=%s AND content_hash=%s",
                (lease.revision_id, lease.profile_id, lease.content_hash),
            ).fetchone()
            active = connection.execute(
                "SELECT active FROM embedding_profiles WHERE id=%s", (lease.profile_id,),
            ).fetchone()["active"]
        after = _vector_text(db, memory.revision_id)
        if verdict == "committed-during-wait":
            assert active is False
            assert job["status"] != "COMPLETED"
            assert after == before
            assert _committed_events(db) == events_before
        else:
            assert verdict == "blocked-by-commit-lock"
            assert job["status"] == "COMPLETED"
            assert after != before
            assert _committed_events(db) == events_before + 1
            assert active is False
    finally:
        with db.transaction() as connection:
            connection.execute(
                "UPDATE embedding_profiles SET active=true WHERE id=%s", (lease.profile_id,),
            )
        db.close()


def test_revision_change_cannot_land_under_a_waiting_vector_write(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database
    from app.embedding_worker import EmbeddingWorker

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    key = uuid.uuid4().hex
    memory = _linked_memory(db, tmp_path, key=key, title="revision race")
    EmbeddingWorker(db, FixedProvider(), owner="embedding-revision-seed").run_once()
    before = _vector_text(db, memory.revision_id)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE embedding_jobs SET status='QUEUED',lease_owner=NULL,lease_until=NULL,"
            "available_at=clock_timestamp() WHERE revision_id=%s",
            (memory.revision_id,),
        )
    worker = EmbeddingWorker(db, SignedProvider(-1.0), owner="embedding-revision-race")
    lease = worker.claim()
    assert lease is not None
    events_before = _committed_events(db)
    replacement = f"memory_revision_{uuid.uuid4().hex}"

    def replace_revision():
        import psycopg

        with psycopg.connect(migrated_postgres_url, autocommit=False) as connection:
            connection.execute("SET application_name = 'embedding-source-contender'")
            connection.execute(
                """
                INSERT INTO memory_revisions(
                  id,entry_id,revision_no,operation,content,content_hash,base_revision_id,actor,created_at
                ) VALUES (%s,%s,%s,'UPDATE',%s,%s,%s,'user',clock_timestamp()::text)
                """,
                (
                    replacement, memory.id, memory.revision_no + 1,
                    f"revised {key}", f"sha256:race-{key}", memory.revision_id,
                ),
            )
            connection.execute(
                "UPDATE memory_entries SET current_revision_id=%s WHERE id=%s",
                (replacement, memory.id),
            )
            connection.commit()

    verdict = _race_commit(migrated_postgres_url, lease, worker, replace_revision)
    with db.connection() as connection:
        job = connection.execute(
            "SELECT status FROM embedding_jobs WHERE revision_id=%s AND profile_id=%s AND content_hash=%s",
            (lease.revision_id, lease.profile_id, lease.content_hash),
        ).fetchone()
        current = connection.execute(
            "SELECT current_revision_id FROM memory_entries WHERE id=%s", (memory.id,),
        ).fetchone()["current_revision_id"]
    after = _vector_text(db, memory.revision_id)
    assert current == replacement
    if verdict == "committed-during-wait":
        assert job["status"] != "COMPLETED"
        assert after == before
        assert _committed_events(db) == events_before
    else:
        assert verdict == "blocked-by-commit-lock"
        assert job["status"] == "COMPLETED"
        assert after != before
        assert _committed_events(db) == events_before + 1
    db.close()
