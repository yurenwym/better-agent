"""PG01: one isolated PostgreSQL lifecycle shared by every backend test scope.

Integration tests import these fixtures through ``integration/conftest.py``;
ordinary tests opt in by requesting ``postgres_url``, ``migrated_postgres_url``
or ``isolated_postgres_test`` from ``tests/conftest.py``. Pure tests never pay
for Docker because session fixtures are instantiated lazily.

Isolation contract
------------------
* A configured ``TEST_DATABASE_URL`` is only a *base*. The fixture allocates a
  uniquely named owned database **before** creating it, keeps that allocation
  registered, and can therefore always attempt a bounded drop - even when the
  create/close/validation step fails half way.
* ``drop_owned_database`` only accepts an object this process allocated. It
  refuses the configured base, and it cannot drop an arbitrary suffix-safe name.
* The Docker path starts one uniquely named compose project with its own tmpfs
  data directory and its own published port.
* Every mutation is preceded by ``assert_isolated_test_database``.
* Connections and cleanup statements are bounded (connect/statement/lock
  timeout); safe DSN query options (e.g. ``sslmode``) survive the path swap and
  routing/override options are refused by the target guard.
* Setup and teardown failures raise ``pytest.fail`` so pytest exits non-zero;
  the primary error is preserved when cleanup also fails.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from integration.db_target_guard import (  # noqa: E402
    UnsafeDatabaseTarget,
    assert_isolated_test_database,
)

COMPOSE_FILE = _TESTS_DIR / "integration" / "compose.postgres.yaml"
BACKEND_ROOT = _TESTS_DIR.parent
POSTGRES_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")

#: Bounds so a wedged server cannot hang cleanup. Docker commands are bounded too.
CONNECT_TIMEOUT_SECONDS = 10
STATEMENT_TIMEOUT_MS = 15_000
LOCK_TIMEOUT_MS = 10_000
COMPOSE_TIMEOUT_SECONDS = 120
MIGRATION_TIMEOUT_SECONDS = 60

#: libpq query keys that re-target the connection; the guard rejects them in the
#: base URL, and they are never copied onto the owned database URL.
ROUTING_QUERY_KEYS = frozenset(
    {"dbname", "host", "hostaddr", "port", "service", "servicefile", "options"}
)

#: Allocations made by this process, so a drop is bound to a real allocation.
_LIVE_ALLOCATIONS: dict[str, "OwnedDatabase"] = {}

#: Whether this process actually created the database. A pre-existing name, a
#: known CREATE rejection and a transport-ambiguous failure are all distinct:
#: only a proven creation may ever be dropped.
_CREATION_STATE: dict[str, "_AllocationState"] = {}


@dataclass
class _AllocationState:
    created: bool = False
    ambiguous: bool = False


class RedactedDatabaseUrl(str):
    def __repr__(self) -> str:
        parsed = urlsplit(_psycopg_dsn(self))
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        username = f"{parsed.username}:***@" if parsed.username else ""
        return repr(urlunsplit((parsed.scheme, f"{username}{hostname}{port}", parsed.path, "", "")))


@dataclass(frozen=True)
class OwnedDatabase:
    base_url: str
    name: str
    url: str


def _psycopg_dsn(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url.removeprefix("postgresql+psycopg://")
    return str(database_url)


def _database_name(database_url: str) -> str:
    return unquote(urlsplit(_psycopg_dsn(database_url)).path.lstrip("/"))


def require_test_database(database_url: str) -> None:
    """Refuse a non-test target before any create, migration or truncate."""
    try:
        assert_isolated_test_database(database_url)
    except UnsafeDatabaseTarget as exc:
        pytest.fail(str(exc), pytrace=False)


def _connect(database_url: str, **extra):
    kwargs = {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "options": (
            f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
            f"-c lock_timeout={LOCK_TIMEOUT_MS}"
        ),
    }
    kwargs.update(extra)
    return psycopg.connect(_psycopg_dsn(database_url), **kwargs)


def _admin_connection(database_url: str):
    return _connect(database_url, autocommit=True)


def run_compose(project: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", project, *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=COMPOSE_TIMEOUT_SECONDS,
    )


def stop_compose(project: str) -> None:
    """Bounded, non-raising compose down; raises RuntimeError on failure."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", project, "down", "-v"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMPOSE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"docker compose down timed out after {COMPOSE_TIMEOUT_SECONDS}s: {exc}"
        ) from exc
    if result.returncode:
        raise RuntimeError(
            f"docker compose down exit {result.returncode}: {(result.stderr or '')[-2000:]}"
        )


def owned_database_name(base_name: str) -> str:
    """A full-UUID name (128-bit) that still fits PostgreSQL's 63-byte limit."""
    stem = re.sub(r"[^A-Za-z0-9_]", "_", base_name).strip("_")[:25] or "better_agent"
    return f"{stem}_{uuid.uuid4().hex}_test"


def _with_database(database_url: str, name: str) -> str:
    """Replace only the database path; keep safe query options (sslmode, …)."""
    parsed = urlsplit(_psycopg_dsn(database_url))
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in ROUTING_QUERY_KEYS
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + name, urlencode(kept), ""))


def allocate_owned_database(base_url: str) -> OwnedDatabase:
    """Register ownership before any CREATE so cleanup can always find it."""
    require_test_database(base_url)
    base_name = _database_name(base_url)
    name = owned_database_name(base_name)
    if name == base_name:
        raise ValueError("owned database must differ from the configured base")
    owned = OwnedDatabase(str(base_url), name, _with_database(base_url, name))
    require_test_database(owned.url)
    _LIVE_ALLOCATIONS[owned.name] = owned
    _CREATION_STATE[owned.name] = _AllocationState()
    return owned


def _require_owned(owned: OwnedDatabase) -> None:
    require_test_database(owned.base_url)
    require_test_database(owned.url)
    if owned.name == _database_name(owned.base_url):
        raise ValueError("refusing to operate on the configured base database")
    if owned.url != _with_database(owned.base_url, owned.name):
        raise ValueError("owned database URL does not match its allocation")
    if _LIVE_ALLOCATIONS.get(owned.name) != owned:
        raise ValueError("refusing a database this process did not allocate")
    if owned.name not in _CREATION_STATE:
        raise ValueError("refusing a database without creation state")


def create_owned_database(owned: OwnedDatabase) -> str:
    """Create only the registered allocation and record whether it succeeded.

    ``created`` is set immediately after a successful CREATE, before the admin
    connection closes, so a close failure still permits a proven cleanup. A
    ``DuplicateDatabase`` means the name already existed and must never be
    dropped; any other failure is transport-ambiguous, so ownership cannot be
    proven and cleanup refuses rather than guessing.
    """
    _require_owned(owned)
    state = _CREATION_STATE[owned.name]
    admin = _admin_connection(owned.base_url)
    try:
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(owned.name)))
            state.created = True
        except psycopg.errors.DuplicateDatabase:
            state.created = False
            raise
        except BaseException:
            state.ambiguous = True
            raise
    finally:
        admin.close()
    return owned.url


def drop_owned_database(owned: OwnedDatabase) -> None:
    """Drop only a proven creation; never a pre-existing or ambiguous database."""
    _require_owned(owned)
    state = _CREATION_STATE[owned.name]
    try:
        if state.ambiguous:
            raise RuntimeError(
                f"cannot prove this process created {owned.name!r}; refusing to drop it"
            )
        if not state.created:
            return  # a known CREATE rejection created nothing
        admin = _admin_connection(owned.base_url)
        try:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(owned.name))
            )
        finally:
            admin.close()
    finally:
        _LIVE_ALLOCATIONS.pop(owned.name, None)
        _CREATION_STATE.pop(owned.name, None)


def truncate_application_tables(database_url: str) -> None:
    require_test_database(database_url)
    with _connect(database_url, row_factory=dict_row) as connection:
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


@contextmanager
def isolated_database_lifecycle(database_url: str):
    """Truncate before and after the test; cleanup runs even if setup fails."""
    primary: BaseException | None = None
    try:
        truncate_application_tables(database_url)
        yield
    except BaseException as exc:  # preserve the test/setup error
        primary = exc
        raise
    finally:
        try:
            truncate_application_tables(database_url)
        except BaseException as cleanup_exc:
            if primary is not None:
                primary.add_note(f"post-test cleanup also failed: {cleanup_exc}")
            else:
                raise


@pytest.fixture(scope="session")
def postgres_url() -> RedactedDatabaseUrl:
    """An owned, disposable PostgreSQL DSN; the configured base is never mutated."""
    configured = os.getenv("TEST_DATABASE_URL")
    if configured:
        if not configured.startswith(POSTGRES_PREFIXES):
            pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL", pytrace=False)
        require_test_database(configured)
        owned: OwnedDatabase | None = None
        primary: BaseException | None = None
        try:
            owned = allocate_owned_database(configured)
            try:
                create_owned_database(owned)
            except BaseException as exc:
                pytest.fail(f"could not create the owned test database: {exc}", pytrace=False)
            yield RedactedDatabaseUrl(owned.url)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if owned is not None:
                try:
                    drop_owned_database(owned)
                except BaseException as cleanup_exc:
                    if primary is not None:
                        primary.add_note(f"owned test database cleanup also failed: {cleanup_exc}")
                    else:
                        pytest.fail(
                            "owned test database cleanup failed: " + str(cleanup_exc),
                            pytrace=False,
                        )
        return

    project = f"better-agent-pgtest-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    primary = None
    try:
        try:
            run_compose(project, "up", "-d", "--wait", "postgres")
            published = run_compose(project, "port", "postgres", "5432").stdout.strip()
            match = re.search(r":(\d+)$", published)
            if match is None:
                pytest.fail(f"could not parse PostgreSQL port from {published!r}", pytrace=False)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            pytest.fail(f"Docker pgvector test service could not start: {exc}", pytrace=False)
        yield RedactedDatabaseUrl(
            "postgresql://better_agent_test:better_agent_test"
            f"@127.0.0.1:{match.group(1)}/better_agent_test"
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            stop_compose(project)
        except BaseException as cleanup_exc:
            if primary is not None:
                primary.add_note(f"isolated PostgreSQL cleanup also failed: {cleanup_exc}")
            else:
                pytest.fail(
                    "isolated PostgreSQL cleanup failed: " + str(cleanup_exc),
                    pytrace=False,
                )


@pytest.fixture(scope="session")
def migrated_postgres_url(postgres_url: RedactedDatabaseUrl) -> RedactedDatabaseUrl:
    # Re-checked here so the rejection sits adjacent to the mutation, not merely early.
    require_test_database(str(postgres_url))
    environment = os.environ.copy()
    environment["DATABASE_URL"] = postgres_url
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(BACKEND_ROOT / "alembic.ini"), "upgrade", "head"],
            cwd=BACKEND_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=MIGRATION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"Isolated PostgreSQL migration timed out after {MIGRATION_TIMEOUT_SECONDS}s: {exc}",
            pytrace=False,
        )
    if result.returncode:
        detail = result.stderr
        for key, value in environment.items():
            if value and ("KEY" in key or "SECRET" in key or "TOKEN" in key or "PASSWORD" in key or "DATABASE_URL" in key):
                detail = detail.replace(value, "<redacted>")
        pytest.fail("Isolated PostgreSQL migration failed:\n" + detail[-5000:], pytrace=False)
    return postgres_url


@pytest.fixture()
def isolated_postgres_test(migrated_postgres_url: RedactedDatabaseUrl):
    """Opt-in empty application-data schema for ordinary tests."""
    with isolated_database_lifecycle(str(migrated_postgres_url)):
        yield
