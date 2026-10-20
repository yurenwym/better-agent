"""Narrow safety checks for the shared PG02 Learning runtime helper."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.startup as startup
import pg02_learning_helpers as helpers
from pg02_learning_helpers import (
    build_pg_runtime,
    close_learning_runtimes,
    database,
    isolated_learning_env,
)


def test_learning_resources_stop_before_pool_close():
    order: list[str] = []

    class Worker:
        def __init__(self, name):
            self.name = name
            self._task = object()

        async def stop(self):
            order.append(self.name)

    class McpSync:
        async def close(self):
            order.append("mcp")

    class Client:
        def close(self):
            order.append("client")

    class PooledDatabase:
        _pool = object()

        def close(self):
            order.append("db")

    runtime = SimpleNamespace(
        db=PooledDatabase(),
        mcp_sync=McpSync(),
        archive_worker=Worker("archive"),
        embedding_client=Client(),
        turn_worker=Worker("turn"),
    )
    helpers._RUNTIMES.append(runtime)
    helpers._ALLOCATED.append(runtime.db)

    assert helpers._close_learning_resources() == []
    assert order == ["mcp", "archive", "client", "turn", "db"]


def test_cleanup_failures_are_aggregated():
    class BrokenWorker:
        _task = object()

        async def stop(self):
            raise RuntimeError("worker boom")

    class BrokenMcp:
        async def close(self):
            raise RuntimeError("mcp boom")

    class BrokenDatabase:
        _pool = object()

        def close(self):
            raise RuntimeError("db boom")

    runtime = SimpleNamespace(
        db=BrokenDatabase(), archive_worker=BrokenWorker(), mcp_sync=BrokenMcp(),
    )
    helpers._RUNTIMES.append(runtime)
    helpers._ALLOCATED.append(runtime.db)

    failures = helpers._close_learning_resources()
    assert any("mcp boom" in failure for failure in failures)
    assert any("worker boom" in failure for failure in failures)
    assert any("db boom" in failure for failure in failures)


def test_cleanup_failure_is_raised_from_fixture(monkeypatch):
    request = SimpleNamespace(fixturenames=[])
    fixture = close_learning_runtimes.__wrapped__(request, monkeypatch)
    next(fixture)

    class BrokenDatabase:
        _pool = object()

        def close(self):
            raise RuntimeError("fixture boom")

    helpers._ALLOCATED.append(BrokenDatabase())
    with pytest.raises(RuntimeError, match="fixture boom"):
        next(fixture)


def test_build_pg_runtime_closes_real_pool_on_success(database, tmp_path):
    runtime = build_pg_runtime(tmp_path, database)
    pool = runtime.db._pool

    for name in helpers._PRE_CLIENT_ATTRS + helpers._POST_CLIENT_ATTRS:
        worker = getattr(runtime, name, None)
        assert worker is None or getattr(worker, "_task", None) is None

    assert pool.closed is False
    assert helpers._close_learning_resources() == []
    assert pool.closed is True


def test_setup_failure_after_database_creation_closes_real_pool(
    database, tmp_path, monkeypatch,
):
    captured = {}

    def failing_event_store(db):
        captured["db"] = db
        raise RuntimeError("injected after database creation")

    monkeypatch.setattr(startup, "EventStore", failing_event_store)

    with pytest.raises(RuntimeError, match="injected after database creation"):
        build_pg_runtime(tmp_path, database)

    allocated = captured["db"]
    pool = allocated._pool
    assert pool.closed is False
    assert helpers._close_learning_resources() == []
    assert pool.closed is True


def test_late_failure_after_embedding_client_creation_closes_client(
    database, tmp_path, monkeypatch,
):
    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "test-key")

    def failing_memory_context(db, embedding_provider=None):
        raise RuntimeError("late embedding failure")

    monkeypatch.setattr(startup, "MemoryContextProvider", failing_memory_context)

    with pytest.raises(RuntimeError, match="late embedding failure"):
        build_pg_runtime(tmp_path, database)

    runtime = helpers._RUNTIMES[-1]
    client = runtime.embedding_client
    pool = runtime.db._pool
    assert client is not None
    assert client._client.is_closed is False
    assert pool.closed is False

    assert helpers._close_learning_resources() == []
    assert client._client.is_closed is True
    assert pool.closed is True
