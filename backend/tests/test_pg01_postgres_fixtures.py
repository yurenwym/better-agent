"""PG01: real PostgreSQL fixture behaviour (opt-in; starts one compose project)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import psycopg
import pytest
from psycopg import sql

from pg_fixtures import allocate_owned_database, create_owned_database, drop_owned_database


def _insert_thread(database, thread_id: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,created_at,updated_at) VALUES (?,?,?,?)",
            (thread_id, "pg01", "2026-10-19T00:00:00Z", "2026-10-19T00:00:00Z"),
        )


def test_two_independent_connections_share_the_owned_database(migrated_postgres_url, tmp_path):
    from app.db import Database

    with closing(Database(migrated_postgres_url, workspace=tmp_path / "one")) as one, \
         closing(Database(migrated_postgres_url, workspace=tmp_path / "two")) as two:
        _insert_thread(one, "pg01-two-connections")
        with two.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM threads WHERE id=?", ("pg01-two-connections",)
            ).fetchone()
    assert row["count"] == 1


def test_opt_in_fixture_exposes_rows_inside_the_test(isolated_postgres_test, migrated_postgres_url, tmp_path):
    from app.db import Database

    with closing(Database(migrated_postgres_url, workspace=tmp_path / "workspace")) as db:
        _insert_thread(db, "pg01-opt-in-row")
    with psycopg.connect(str(migrated_postgres_url)) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM threads WHERE id='pg01-opt-in-row'"
        ).fetchone()[0]
    assert count == 1


def test_opt_in_fixture_starts_each_test_empty(isolated_postgres_test, migrated_postgres_url):
    with psycopg.connect(str(migrated_postgres_url)) as connection:
        count = connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    assert count == 0


def test_owned_database_is_created_and_removed(migrated_postgres_url):
    base = str(migrated_postgres_url)
    owned = allocate_owned_database(base)
    try:
        create_owned_database(owned)
        with psycopg.connect(owned.url) as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        drop_owned_database(owned)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(owned.url, connect_timeout=2)


def test_two_concurrent_owned_allocations_against_one_base_are_isolated_and_cleaned(
    migrated_postgres_url,
):
    """Helper-level concurrency: two owned allocations share one dedicated base.

    The coordinator separately validated two full concurrent pytest sessions
    (pg01-configured-concurrency.json); this focused test keeps the same-table
    row, untouched-sentinel and cleanup claims executable without a second
    pytest process.
    """
    base = str(migrated_postgres_url)
    sentinel_table = "pg01_sentinel"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(
            f"CREATE TABLE IF NOT EXISTS {sentinel_table}"
            "(id text PRIMARY KEY, value text NOT NULL)"
        )
        admin.execute(f"TRUNCATE {sentinel_table}")
        admin.execute(f"INSERT INTO {sentinel_table}(id,value) VALUES ('base','unchanged')")

    def session(index: int) -> tuple[str, str, int]:
        owned = allocate_owned_database(base)
        try:
            create_owned_database(owned)
            with psycopg.connect(owned.url) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS probe(id text PRIMARY KEY)")
                connection.execute("INSERT INTO probe(id) VALUES (%s)", (f"worker-{index}",))
                connection.commit()
                count = connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
            return owned.url, f"worker-{index}", count
        finally:
            drop_owned_database(owned)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(session, range(2)))

        urls = [url for url, _, _ in results]
        identifiers = [identifier for _, identifier, _ in results]
        assert len(set(urls)) == 2, "each session must own a distinct database"
        assert len(set(identifiers)) == 2, "the same logical table holds distinct rows"
        assert [count for _, _, count in results] == [1, 1]

        with psycopg.connect(base) as connection:
            sentinel = connection.execute(
                f"SELECT value FROM {sentinel_table} WHERE id='base'"
            ).fetchone()[0]
        assert sentinel == "unchanged", "the shared base must not be mutated"

        for url in urls:
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(url, connect_timeout=2)
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(f"DROP TABLE IF EXISTS {sentinel_table}")


def test_preexisting_allocated_name_survives_real_create_collision(migrated_postgres_url):
    """A same-name database that existed before CREATE must never be dropped."""
    base = str(migrated_postgres_url)
    owned = allocate_owned_database(base)
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(owned.name)))
    try:
        with pytest.raises(psycopg.errors.DuplicateDatabase):
            create_owned_database(owned)
        drop_owned_database(owned)  # known rejection: must not drop the pre-existing DB
        with psycopg.connect(owned.url, connect_timeout=2) as connection:
            assert connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(owned.name)
                )
            )
