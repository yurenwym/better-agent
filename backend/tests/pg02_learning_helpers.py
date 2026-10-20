"""Small PostgreSQL helpers for the PG02 batch-3 Learning modules.

Reuses the accepted ``pg02_helpers.database`` fixture and supplies a
``build_runtime`` that takes an explicit PostgreSQL URL instead of relying on
ambient SQLite configuration.

The helper also owns the test-local lifecycle of every database pool that
``build_runtime`` allocates, including allocations made before startup fails.
Resources are stopped before their pools close, and the safety fixture depends
on the shared ``database`` fixture so this teardown runs before the fixture
pool and owned database are released. Cleanup failures are aggregated and
raised instead of being swallowed.
"""
from __future__ import annotations

import asyncio

import pytest

from app.startup import build_runtime
from pg02_helpers import database

__all__ = [
    "build_pg_runtime",
    "close_learning_runtimes",
    "database",
    "isolated_learning_env",
]

_AMBIENT_ENV = (
    "EMBEDDING_BASE_URL", "MEMORY_EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL", "MEMORY_EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS", "MEMORY_EMBEDDING_DIMENSIONS",
    "EMBEDDING_TIMEOUT_SECONDS", "MEMORY_EMBEDDING_TIMEOUT_SECONDS",
    "EMBEDDING_API_KEY_ENV", "MEMORY_EMBEDDING_API_KEY_ENV",
    "EMBEDDING_API_KEY", "MEMORY_EMBEDDING_API_KEY",
    "EMBEDDING_PROVIDER_NAME", "MEMORY_EMBEDDING_PROVIDER_NAME",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "AGENT_MODEL_API_KEY", "AGENT_FALLBACK_MODEL_API_KEY",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)

_STOP_TIMEOUT = 10.0
_PRE_CLIENT_ATTRS = (
    "scheduler", "evaluation_worker", "archive_worker", "embedding_worker",
)
_POST_CLIENT_ATTRS = (
    "research_worker", "goal_review_worker", "agent_worker",
    "observer_worker", "turn_worker",
)

_ALLOCATED: list = []
_RUNTIMES: list = []
_CLOSED: set[int] = set()


@pytest.fixture(autouse=True)
def isolated_learning_env(monkeypatch):
    for name in _AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def close_learning_runtimes(request, monkeypatch):
    """Track and close Learning databases in dependency order.

    Requesting ``database`` only for tests that actually use it keeps pure
    tests free of PostgreSQL while still making this fixture finalize before
    the shared pool.
    """
    import app.startup as startup

    original_database = startup.Database

    class TrackedDatabase(original_database):
        def __init__(self, *args, **kwargs):
            _ALLOCATED.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(startup, "Database", TrackedDatabase)

    original_runtime = startup.AgentRuntime

    class TrackedRuntime(original_runtime):
        def __init__(self, *args, **kwargs):
            _RUNTIMES.append(self)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(startup, "AgentRuntime", TrackedRuntime)

    if "database" in request.fixturenames:
        request.getfixturevalue("database")

    _ALLOCATED.clear()
    _RUNTIMES.clear()
    _CLOSED.clear()
    yield
    failures = _close_learning_resources()
    if failures:
        raise RuntimeError("Learning runtime cleanup failed: " + "; ".join(failures))


def build_pg_runtime(tmp_path, database, **kwargs):
    runtime = build_runtime(tmp_path, database_url=database.database_url, **kwargs)
    if not any(item is runtime for item in _RUNTIMES):
        _RUNTIMES.append(runtime)
    return runtime


def _stop_async(worker, name: str, failures: list[str]) -> None:
    try:
        asyncio.run(asyncio.wait_for(worker.stop(), timeout=_STOP_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 - collected for the caller
        failures.append(f"{name}.stop failed: {exc!r}")


def _close_async(obj, name: str, method: str, failures: list[str]) -> None:
    try:
        asyncio.run(asyncio.wait_for(getattr(obj, method)(), timeout=_STOP_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 - collected for the caller
        failures.append(f"{name}.{method} failed: {exc!r}")


def _stop_runtime(runtime) -> list[str]:
    failures: list[str] = []

    sync = getattr(runtime, "mcp_sync", None)
    if sync is not None:
        _close_async(sync, "mcp_sync", "close", failures)
    else:
        manager = getattr(runtime, "mcp_manager", None)
        if manager is not None:
            _close_async(manager, "mcp_manager", "close", failures)

    for name in _PRE_CLIENT_ATTRS:
        worker = getattr(runtime, name, None)
        if worker is not None and getattr(worker, "_task", None) is not None:
            _stop_async(worker, name, failures)

    client = getattr(runtime, "embedding_client", None)
    if client is not None:
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 - collected for the caller
            failures.append(f"embedding_client.close failed: {exc!r}")

    for name in _POST_CLIENT_ATTRS:
        worker = getattr(runtime, name, None)
        if worker is not None and getattr(worker, "_task", None) is not None:
            _stop_async(worker, name, failures)
    return failures


def _close_database(database) -> list[str]:
    if id(database) in _CLOSED:
        return []
    _CLOSED.add(id(database))
    if getattr(database, "_pool", "unset") is None:
        return []
    try:
        database.close()
    except Exception as exc:  # noqa: BLE001 - collected for the caller
        return [f"database.close failed: {exc!r}"]
    return []


def _close_learning_resources() -> list[str]:
    failures: list[str] = []
    for runtime in reversed(_RUNTIMES):
        failures.extend(_stop_runtime(runtime))
        database = getattr(runtime, "db", None)
        if database is not None:
            failures.extend(_close_database(database))
    for database in reversed(_ALLOCATED):
        failures.extend(_close_database(database))
    _RUNTIMES.clear()
    _ALLOCATED.clear()
    return failures
