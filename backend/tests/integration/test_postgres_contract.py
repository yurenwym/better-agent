from __future__ import annotations

from pathlib import Path
from contextlib import closing

from alembic.config import Config
from alembic.script import ScriptDirectory
import psycopg
import pytest


def test_alembic_installs_required_postgres_extensions(migrated_postgres_url) -> None:
    with psycopg.connect(migrated_postgres_url, row_factory=psycopg.rows.dict_row) as connection:
        extensions = connection.execute(
            "SELECT extname, extversion FROM pg_extension "
            "WHERE extname IN ('vector', 'pg_trgm') ORDER BY extname"
        ).fetchall()
        revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()

    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    expected_heads = set(ScriptDirectory.from_config(config).get_heads())

    assert [row["extname"] for row in extensions] == ["pg_trgm", "vector"]
    assert all(row["extversion"] for row in extensions)
    assert {row["version_num"] for row in revisions} == expected_heads


def test_database_uses_postgresql_and_pgvector(migrated_postgres_url, tmp_path) -> None:
    from app.db import Database

    with closing(Database(migrated_postgres_url, workspace=tmp_path / "workspace")) as db:
        with db.connection() as connection:
            backend = connection.execute(
                "SELECT current_setting('server_version_num') AS version"
            ).fetchone()
            vector = connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()

        assert int(backend["version"]) >= 160000
        assert vector is not None
        assert vector["extversion"]
        assert db.backend == "postgresql"
    db.close()


def test_database_postgres_transaction_rolls_back_and_rows_keep_compat_access(
    migrated_postgres_url, tmp_path
) -> None:
    from app.db import Database

    with psycopg.connect(migrated_postgres_url, autocommit=True) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS integration_transaction_probe "
            "(id text PRIMARY KEY, value integer NOT NULL)"
        )
        connection.execute("TRUNCATE integration_transaction_probe")

    with closing(Database(migrated_postgres_url, workspace=tmp_path / "workspace")) as db:
        with pytest.raises(RuntimeError, match="force rollback"):
            with db.transaction() as connection:
                connection.execute(
                    "INSERT INTO integration_transaction_probe(id, value) VALUES (%s, %s)",
                    ("rolled-back", 7),
                )
                raise RuntimeError("force rollback")

        with db.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM integration_transaction_probe"
            ).fetchone()

    assert row["count"] == row[0] == 0
    db.close()


def test_postgres_event_ledgers_are_append_only(migrated_postgres_url) -> None:
    with psycopg.connect(migrated_postgres_url) as connection:
        connection.execute(
            "INSERT INTO threads(id,title,created_at,updated_at) "
            "VALUES ('append-thread','append','2026-09-06','2026-09-06')"
        )
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
            "VALUES ('append-turn','append-thread','append-client','ACCEPTED','2026-09-06','2026-09-06')"
        )
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json) "
            "VALUES (1,'append-event',1,'append-thread','append-turn','turn.accepted','2026-09-06','test','{}')"
        )

    with psycopg.connect(migrated_postgres_url) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute(
                "UPDATE thread_events SET actor='changed' WHERE event_id='append-event'"
            )

    with psycopg.connect(migrated_postgres_url) as connection:
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            connection.execute("DELETE FROM thread_events WHERE event_id='append-event'")
