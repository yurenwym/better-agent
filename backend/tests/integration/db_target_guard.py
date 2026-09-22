"""T01: fail-closed guard for the PostgreSQL integration target.

Why this exists
---------------
`isolated_postgres_test` in `conftest.py` runs
`TRUNCATE ... RESTART IDENTITY CASCADE` on every application table around each
test. Pointing `TEST_DATABASE_URL` at the development database therefore destroys
real data. The 2026-09-22 baseline measured `better_agent` holding 46 threads,
305 thread messages, 7 goals, 7 runs and 35 model profiles.

The original fixture accepted any DSN that merely started with `postgresql://`,
so the only thing standing between the suite and the development database was
the operator remembering to type the right name.

Ordering requirement
--------------------
The guard must run *before* `alembic upgrade` and before the first TRUNCATE — a
check that fires after the migration has already rewritten the schema is useless.
`conftest.py` therefore calls it in three places:

1. when `TEST_DATABASE_URL` is read (earliest possible rejection);
2. immediately before invoking alembic (immediately adjacent to the mutation);
3. inside the truncate helper itself (defence in depth if it is ever called
   from elsewhere).
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, unquote, urlsplit

#: A database name must end with one of these to count as a test database.
TEST_DATABASE_SUFFIXES = ("_test", "_tests", "_pgtest")

#: Comma-separated explicit registrations, for names that break the suffix convention.
TEST_DATABASE_ALLOWLIST_ENV = "TEST_DATABASE_ALLOW"

#: The fixture connects over TCP; a non-loopback host means a shared or remote database.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Application databases that must never be migrated or truncated by the suite.
FORBIDDEN_DATABASE_NAMES = frozenset(
    {"better_agent", "postgres", "template0", "template1"}
)


class UnsafeDatabaseTarget(RuntimeError):
    """Raised when a DSN is not an explicitly registered test database."""


def redact(database_url: str) -> str:
    """Render a DSN with the password and query string removed."""
    parsed = urlsplit(database_url)
    credentials = f"{parsed.username}:***@" if parsed.username else ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{credentials}{host}{port}{parsed.path}"


def registered_test_databases() -> set[str]:
    """Names explicitly registered through `TEST_DATABASE_ALLOW`."""
    raw = os.getenv(TEST_DATABASE_ALLOWLIST_ENV, "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def describe_unsafe_target(database_url: str) -> list[str]:
    """Reasons this DSN is unsafe to migrate or truncate. Empty list means safe."""
    parsed = urlsplit(database_url)
    name = unquote(parsed.path.lstrip("/"))
    host = (parsed.hostname or "").lower()
    problems: list[str] = []

    # libpq query options override the authority/path (including dbname).
    # Reject routing overrides rather than checking a different target from
    # the driver. service files and options can also change the target/schema.
    routing_options = {"dbname", "host", "hostaddr", "port", "service", "servicefile", "options"}
    if any(key.lower() in routing_options for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        problems.append("connection routing overrides are not allowed in test URLs")

    if not name:
        problems.append("the URL names no database")
    elif name in FORBIDDEN_DATABASE_NAMES:
        problems.append(
            f"database {name!r} is an application database, not a test database"
        )
    elif name not in registered_test_databases() and not name.endswith(
        TEST_DATABASE_SUFFIXES
    ):
        problems.append(
            f"database {name!r} is not registered as a test database "
            f"(the name must end with one of {TEST_DATABASE_SUFFIXES}, "
            f"or be listed in {TEST_DATABASE_ALLOWLIST_ENV})"
        )

    if host not in LOOPBACK_HOSTS:
        problems.append(f"host {host or '<none>'!r} is not a loopback address")

    return problems


def assert_isolated_test_database(database_url: str) -> None:
    """Raise `UnsafeDatabaseTarget` unless the DSN is a registered test database."""
    problems = describe_unsafe_target(database_url)
    if problems:
        raise UnsafeDatabaseTarget(
            "refusing to migrate or truncate a non-test database: "
            + "; ".join(problems)
            + f" [target={redact(database_url)}]"
        )
