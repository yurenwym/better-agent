from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from db_target_guard import UnsafeDatabaseTarget, assert_isolated_test_database


COMPOSE_FILE = Path(__file__).with_name("compose.postgres.yaml")
BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _require_test_database(database_url: str) -> None:
    """Reject a non-test target *before* alembic or TRUNCATE can touch it.

    `isolated_postgres_test` truncates every application table around each test,
    so a mis-pointed `TEST_DATABASE_URL` would destroy the development database.
    See `db_target_guard` for the rules and why the ordering matters.
    """
    try:
        assert_isolated_test_database(database_url)
    except UnsafeDatabaseTarget as exc:
        pytest.fail(str(exc), pytrace=False)


class RedactedDatabaseUrl(str):
    def __repr__(self) -> str:
        parsed = urlsplit(self)
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        username = f"{parsed.username}:***@" if parsed.username else ""
        return repr(urlunsplit((parsed.scheme, f"{username}{hostname}{port}", parsed.path, "", "")))


def _run_compose(project: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", project, *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )


@pytest.fixture(scope="session")
def postgres_url() -> RedactedDatabaseUrl:
    """Return an isolated pgvector DSN without using application/model secrets."""
    configured = os.getenv("TEST_DATABASE_URL")
    if configured:
        if not configured.startswith(("postgresql://", "postgresql+psycopg://")):
            pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")
        _require_test_database(configured)
        yield RedactedDatabaseUrl(configured)
        return

    project = f"better-agent-pgtest-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        _run_compose(project, "up", "-d", "--wait", "postgres")
        published = _run_compose(project, "port", "postgres", "5432").stdout.strip()
        match = re.search(r":(\d+)$", published)
        if match is None:
            pytest.fail(f"could not parse PostgreSQL port from {published!r}")
        yield RedactedDatabaseUrl(
            "postgresql://better_agent_test:better_agent_test"
            f"@127.0.0.1:{match.group(1)}/better_agent_test"
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"Docker pgvector test service could not start: {exc}")
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", project, "down", "-v"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )


@pytest.fixture(scope="session")
def migrated_postgres_url(postgres_url: RedactedDatabaseUrl) -> RedactedDatabaseUrl:
    # Re-checked here so the rejection sits adjacent to the mutation, not merely early.
    _require_test_database(str(postgres_url))
    environment = os.environ.copy()
    environment["DATABASE_URL"] = postgres_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode:
        detail = result.stderr
        for key, value in environment.items():
            if value and ("KEY" in key or "SECRET" in key or "TOKEN" in key or "PASSWORD" in key or "DATABASE_URL" in key):
                detail = detail.replace(value, "<redacted>")
        pytest.fail("Isolated PostgreSQL migration failed:\n" + detail[-5000:], pytrace=False)
    return postgres_url


def _truncate_application_tables(database_url: str) -> None:
    _require_test_database(database_url)
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        tables = [
            row["tablename"]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema() "
                "AND tablename NOT IN ('alembic_version','embedding_profiles')"
            )
        ]
        if tables:
            connection.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(",").join(map(sql.Identifier, tables))
                )
            )
        connection.execute(
            "INSERT INTO app_settings(id,human_mode,updated_at) "
            "VALUES (1,0,clock_timestamp()::text)"
        )


@pytest.fixture(autouse=True)
def isolated_postgres_test(migrated_postgres_url: RedactedDatabaseUrl):
    """Give every integration test an empty application-data schema."""
    _truncate_application_tables(migrated_postgres_url)
    yield
    _truncate_application_tables(migrated_postgres_url)
