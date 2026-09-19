from __future__ import annotations

import uuid


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
    import pytest

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
    assert first_provider.calls == 1

    second.process(current_lease)
    db.close()
