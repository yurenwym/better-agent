from concurrent.futures import ThreadPoolExecutor
import pytest

from app.db import Database
from app.events import EventStore


def test_concurrent_append_idempotence_and_rollback(isolated_postgres_test, migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        store = EventStore(db)
        def append(index):
            return store.append("event-test-run", "goal", "test", "runtime", {"index": index}, event_id=f"id-{index}")
        with ThreadPoolExecutor(max_workers=6) as executor:
            values = list(executor.map(append, range(18)))
        assert sorted(e.seq for e in values) == list(range(1, 19))
        with ThreadPoolExecutor(max_workers=6) as executor:
            repeats = list(executor.map(append, [0] * 12))
        assert len({e.seq for e in repeats}) == 1
        assert len(store.list("event-test-run")) == 18
        with pytest.raises(ValueError):
            store.append("event-test-run", "goal", "test", "runtime", {"index": 100}, event_id="id-0")
        with pytest.raises(RuntimeError):
            with db.transaction() as conn:
                store.append("event-test-run", "goal", "rolled-back", "runtime", {}, connection=conn)
                raise RuntimeError("rollback")
        assert len(EventStore(db).list("event-test-run")) == 18
        assert all(e.envelope_json is None for e in store.list("event-test-run"))
    finally:
        db.close()


def test_thread_concurrent_append(isolated_postgres_test, migrated_postgres_url, tmp_path):
    from app.events import ThreadEventStore
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        with db.transaction() as conn:
            conn.execute("INSERT INTO threads(id,title,created_at,updated_at) VALUES ('thread','test','2026-10-10','2026-10-10')")
        store = ThreadEventStore(db)
        with ThreadPoolExecutor(max_workers=6) as executor:
            values = list(executor.map(lambda i: store.append("thread", "legacy-turn", "test", "runtime", {"i": i}), range(18)))
        assert sorted(e.seq for e in values) == list(range(1, 19))
        assert len(store.list("thread", after_seq=9)) == 9
    finally:
        db.close()
