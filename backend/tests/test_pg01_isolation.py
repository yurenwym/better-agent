"""PG01: fail-closed, connection-safe PostgreSQL fixture behaviour (no Docker)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

import pg_fixtures
from integration.db_target_guard import describe_unsafe_target
from pg_fixtures import (
    OwnedDatabase,
    _with_database,
    allocate_owned_database,
    create_owned_database,
    drop_owned_database,
    owned_database_name,
)

DEVELOPMENT = "postgresql://better_agent:secret@127.0.0.1:5432/better_agent"
SAFE = "postgresql://u:p@127.0.0.1:5432/better_agent_v3_test"
TESTS_DIR = Path(pg_fixtures.__file__).resolve().parent


class _RecordingAdmin:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def execute(self, statement) -> None:
        self.statements.append(str(statement))

    def close(self) -> None:
        self.statements.append("close")


class _FailingAdmin(_RecordingAdmin):
    def __init__(self, statements: list[str], error: BaseException) -> None:
        super().__init__(statements)
        self.error = error

    def execute(self, statement) -> None:
        self.statements.append(str(statement))
        raise self.error


def _release(owned: OwnedDatabase) -> None:
    pg_fixtures._LIVE_ALLOCATIONS.pop(owned.name, None)
    pg_fixtures._CREATION_STATE.pop(owned.name, None)


def test_owned_names_are_unique_and_pass_the_guard():
    names = {owned_database_name("better_agent_v3_test") for _ in range(25)}
    assert len(names) == 25
    for name in names:
        assert name.endswith("_test")
        assert len(name) <= 63
        assert describe_unsafe_target(f"postgresql://u:p@127.0.0.1:5432/{name}") == []


def test_application_base_is_rejected_before_any_connection(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(pg_fixtures, "_admin_connection", lambda url: calls.append(url))
    with pytest.raises(pytest.fail.Exception):
        allocate_owned_database(DEVELOPMENT)
    assert calls == []


def test_owned_database_is_created_and_dropped_without_mutating_base(monkeypatch):
    statements: list[str] = []
    monkeypatch.setattr(pg_fixtures, "_admin_connection", lambda url: _RecordingAdmin(statements))
    owned = allocate_owned_database(SAFE)
    try:
        create_owned_database(owned)
        assert any("CREATE DATABASE" in statement for statement in statements)
        assert not any("TRUNCATE" in statement or "DROP" in statement for statement in statements)
        assert statements[-1] == "close"

        drop_owned_database(owned)
        assert "DROP DATABASE IF EXISTS" in statements[-2]
        assert f"Identifier('{owned.name}')" in statements[-2]
        assert "Identifier('better_agent_v3_test')" not in statements[-2]
        assert statements[-1] == "close"
        assert owned.name not in pg_fixtures._LIVE_ALLOCATIONS
    finally:
        _release(owned)


def test_preexisting_name_survives_create_collision(monkeypatch):
    statements: list[str] = []
    monkeypatch.setattr(
        pg_fixtures,
        "_admin_connection",
        lambda url: _FailingAdmin(statements, psycopg.errors.DuplicateDatabase("exists")),
    )
    owned = allocate_owned_database(SAFE)
    try:
        with pytest.raises(psycopg.errors.DuplicateDatabase):
            create_owned_database(owned)
        drop_owned_database(owned)  # known rejection: nothing was created
        assert not any("DROP" in statement for statement in statements)
    finally:
        _release(owned)


def test_ambiguous_create_failure_refuses_to_drop(monkeypatch):
    statements: list[str] = []
    monkeypatch.setattr(
        pg_fixtures,
        "_admin_connection",
        lambda url: _FailingAdmin(statements, psycopg.OperationalError("connection lost")),
    )
    owned = allocate_owned_database(SAFE)
    try:
        with pytest.raises(psycopg.OperationalError):
            create_owned_database(owned)
        with pytest.raises(RuntimeError, match="cannot prove"):
            drop_owned_database(owned)
        assert not any("DROP" in statement for statement in statements)
    finally:
        _release(owned)


def test_close_failure_after_create_still_allows_proven_cleanup(monkeypatch):
    statements: list[str] = []

    class CloseFails(_RecordingAdmin):
        def close(self):
            self.statements.append("close-failed")
            raise psycopg.OperationalError("close lost")

    admins = [CloseFails(statements), _RecordingAdmin(statements)]
    monkeypatch.setattr(pg_fixtures, "_admin_connection", lambda url: admins.pop(0))
    owned = allocate_owned_database(SAFE)
    try:
        with pytest.raises(psycopg.OperationalError):
            create_owned_database(owned)
        drop_owned_database(owned)
        assert any("DROP DATABASE IF EXISTS" in statement for statement in statements)
    finally:
        _release(owned)


def test_drop_refuses_base_and_unallocated_names():
    forged_name = "better_agent_v3_test_deadbeef_test"
    forged = OwnedDatabase(SAFE, forged_name, _with_database(SAFE, forged_name))
    with pytest.raises(ValueError):
        drop_owned_database(forged)

    base_owned = OwnedDatabase(SAFE, "better_agent_v3_test", SAFE)
    with pytest.raises(ValueError):
        drop_owned_database(base_owned)


def test_owned_url_preserves_safe_query_options_but_not_routing_ones():
    url = (
        "postgresql://u:p@127.0.0.1:5432/better_agent_v3_test"
        "?sslmode=require&application_name=pg01&options=-c%20x%3D1"
    )
    owned_url = _with_database(url, "owned_test")
    assert owned_url.startswith("postgresql://")
    assert "/owned_test" in owned_url
    assert "sslmode=require" in owned_url
    assert "application_name=pg01" in owned_url
    assert "options=" not in owned_url


def test_migration_refuses_an_unsafe_target_before_alembic(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(pg_fixtures.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(pytest.fail.Exception):
        pg_fixtures.migrated_postgres_url.__wrapped__(DEVELOPMENT)
    assert calls == []


def test_migration_nonzero_is_reported(monkeypatch):
    monkeypatch.setattr(
        pg_fixtures.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "migration-boom"),
    )
    with pytest.raises(pytest.fail.Exception) as excinfo:
        pg_fixtures.migrated_postgres_url.__wrapped__(SAFE)
    message = str(excinfo.value)
    assert "migration failed" in message and "migration-boom" in message


def test_migration_timeout_is_reported(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("alembic", 60)

    monkeypatch.setattr(pg_fixtures.subprocess, "run", timeout)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        pg_fixtures.migrated_postgres_url.__wrapped__(SAFE)
    assert "timed out" in str(excinfo.value)


def test_docker_setup_failure_still_attempts_cleanup(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    calls: list[tuple] = []

    def compose(project, *args):
        calls.append(("compose", args[0]))
        if args[0] == "up":
            raise subprocess.CalledProcessError(1, "docker compose up", stderr="up failed")
        raise AssertionError(f"unexpected compose call: {args}")

    monkeypatch.setattr(pg_fixtures, "run_compose", compose)
    monkeypatch.setattr(pg_fixtures, "stop_compose", lambda project: calls.append(("stop", project)))

    generator = pg_fixtures.postgres_url.__wrapped__()
    with pytest.raises(pytest.fail.Exception) as excinfo:
        next(generator)
    assert "could not start" in str(excinfo.value)
    assert calls[0] == ("compose", "up")
    assert calls[-1][0] == "stop"


def test_docker_port_parse_failure_fails_and_still_cleans(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        pg_fixtures,
        "run_compose",
        lambda project, *args: subprocess.CompletedProcess(args, 0, "nonsense\n", ""),
    )
    monkeypatch.setattr(pg_fixtures, "stop_compose", lambda project: calls.append("stop"))

    generator = pg_fixtures.postgres_url.__wrapped__()
    with pytest.raises(pytest.fail.Exception) as excinfo:
        next(generator)
    assert "could not parse" in str(excinfo.value)
    assert calls == ["stop"]


def test_docker_cleanup_failure_is_visible(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        pg_fixtures,
        "run_compose",
        lambda project, *args: subprocess.CompletedProcess(args, 0, "127.0.0.1:5432\n", ""),
    )

    def stop(project):
        raise RuntimeError("down-boom")

    monkeypatch.setattr(pg_fixtures, "stop_compose", stop)
    generator = pg_fixtures.postgres_url.__wrapped__()
    next(generator)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        next(generator)
    message = str(excinfo.value)
    assert "cleanup failed" in message and "down-boom" in message


def test_configured_base_is_never_yielded_or_mutated(monkeypatch):
    base = SAFE
    owned = OwnedDatabase(
        base,
        "better_agent_v3_test_deadbeef_test",
        "postgresql://u:p@127.0.0.1:5432/better_agent_v3_test_deadbeef_test",
    )
    events: list[tuple] = []
    monkeypatch.setenv("TEST_DATABASE_URL", base)
    monkeypatch.setattr(pg_fixtures, "allocate_owned_database", lambda url: events.append(("allocate", url)) or owned)
    monkeypatch.setattr(pg_fixtures, "create_owned_database", lambda item: events.append(("create", item.name)) or item.url)
    monkeypatch.setattr(pg_fixtures, "drop_owned_database", lambda item: events.append(("drop", item.name)))

    generator = pg_fixtures.postgres_url.__wrapped__()
    assert str(next(generator)) == owned.url
    with pytest.raises(StopIteration):
        next(generator)
    assert events == [("allocate", base), ("create", owned.name), ("drop", owned.name)]


def test_configured_cleanup_failure_fails_closed(monkeypatch):
    base = SAFE
    owned = OwnedDatabase(
        base,
        "better_agent_v3_test_deadbeef_test",
        "postgresql://u:p@127.0.0.1:5432/better_agent_v3_test_deadbeef_test",
    )
    monkeypatch.setenv("TEST_DATABASE_URL", base)
    monkeypatch.setattr(pg_fixtures, "allocate_owned_database", lambda url: owned)
    monkeypatch.setattr(pg_fixtures, "create_owned_database", lambda item: item.url)

    def drop(item):
        raise RuntimeError("drop-boom")

    monkeypatch.setattr(pg_fixtures, "drop_owned_database", drop)
    generator = pg_fixtures.postgres_url.__wrapped__()
    next(generator)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        next(generator)
    message = str(excinfo.value)
    assert "cleanup failed" in message and "drop-boom" in message


def test_configured_create_failure_still_attempts_drop(monkeypatch):
    base = SAFE
    owned = OwnedDatabase(
        base,
        "better_agent_v3_test_deadbeef_test",
        "postgresql://u:p@127.0.0.1:5432/better_agent_v3_test_deadbeef_test",
    )
    dropped: list[str] = []
    monkeypatch.setenv("TEST_DATABASE_URL", base)
    monkeypatch.setattr(pg_fixtures, "allocate_owned_database", lambda url: owned)

    def create(item):
        raise RuntimeError("create-boom")

    monkeypatch.setattr(pg_fixtures, "create_owned_database", create)
    monkeypatch.setattr(pg_fixtures, "drop_owned_database", lambda item: dropped.append(item.name))

    generator = pg_fixtures.postgres_url.__wrapped__()
    with pytest.raises(pytest.fail.Exception) as excinfo:
        next(generator)
    message = str(excinfo.value)
    assert "could not create" in message and "create-boom" in message
    assert dropped == [owned.name]


# --------------------------------------------------------------------------- #
# Actual fixture lifecycle in a pytest subprocess (not a fabricated fixture).
# --------------------------------------------------------------------------- #

def _run_subprocess(tmp_path: Path, conftest: str, env: dict[str, str]):
    (tmp_path / "conftest.py").write_text(conftest, encoding="utf-8")
    (tmp_path / "test_lifecycle.py").write_text(
        "def test_ok(postgres_url):\n    assert postgres_url\n", encoding="utf-8"
    )
    full_env = {key: value for key, value in os.environ.items() if key != "TEST_DATABASE_URL"}
    full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, env=full_env,
    )


def test_actual_configured_teardown_failure_exits_nonzero(tmp_path: Path):
    conftest = f"""
import sys
sys.path.insert(0, {str(TESTS_DIR)!r})
import pg_fixtures

postgres_url = pg_fixtures.postgres_url

class Owned:
    base_url = {SAFE!r}
    name = "better_agent_v3_test_deadbeef_test"
    url = "postgresql://u:p@127.0.0.1:5432/better_agent_v3_test_deadbeef_test"

pg_fixtures.allocate_owned_database = lambda base: Owned()
pg_fixtures.create_owned_database = lambda owned: owned.url

def _drop(owned):
    raise RuntimeError("injected-cleanup-failure")

pg_fixtures.drop_owned_database = _drop
"""
    result = _run_subprocess(tmp_path, conftest, {"TEST_DATABASE_URL": SAFE})
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "owned test database cleanup failed" in output
    assert "injected-cleanup-failure" in output


def test_actual_docker_teardown_failure_exits_nonzero(tmp_path: Path):
    conftest = f"""
import sys
sys.path.insert(0, {str(TESTS_DIR)!r})
import pg_fixtures

postgres_url = pg_fixtures.postgres_url

class Result:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.returncode = 0
        self.stderr = ""

def _compose(project, *args):
    return Result("127.0.0.1:5432\\n") if args[0] == "port" else Result()

def _stop(project):
    raise RuntimeError("injected-down-failure")

pg_fixtures.run_compose = _compose
pg_fixtures.stop_compose = _stop
"""
    result = _run_subprocess(tmp_path, conftest, {})
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "isolated PostgreSQL cleanup failed" in output
    assert "injected-down-failure" in output


def test_actual_docker_setup_failure_still_cleans_in_subprocess(tmp_path: Path):
    marker = tmp_path / "stop-marker"
    conftest = f"""
import subprocess
import sys
sys.path.insert(0, {str(TESTS_DIR)!r})
import pg_fixtures

postgres_url = pg_fixtures.postgres_url
MARKER = {str(marker)!r}

def _compose(project, *args):
    if args[0] == "up":
        raise subprocess.CalledProcessError(1, "docker compose up", stderr="up-failed")
    raise AssertionError("port must not run")

def _stop(project):
    with open(MARKER, "w", encoding="utf-8") as handle:
        handle.write(project)

pg_fixtures.run_compose = _compose
pg_fixtures.stop_compose = _stop
"""
    result = _run_subprocess(tmp_path, conftest, {})
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "could not start" in output
    assert marker.is_file() and marker.read_text(encoding="utf-8")
