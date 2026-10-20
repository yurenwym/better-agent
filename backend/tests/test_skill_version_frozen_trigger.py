"""PG02: the deployed 0035 frozen skill-version trigger contract.

The SQLite schema has frozen skill versions since migration 11; this test
installs a real version on revision 0034, upgrades to the deployed 0035 trigger,
and exercises every immutable column, DELETE, status changes and rollback on the
actual PostgreSQL database.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

import app.db as db_module
import pg_fixtures
from app.db import Database
from app.skill_platform import SkillPlatform
from test_skill_platform import _skill_zip

BACKEND_ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE = (
    "skill_id", "version", "package_digest", "manifest_digest", "manifest_json",
    "title", "description", "content", "requested_tools_json",
    "connectors_json", "phases_json", "storage_path", "created_at",
)


def _upgrade(database_url: str, revision: str) -> None:
    pg_fixtures.require_test_database(database_url)
    result = subprocess.run(
        [
            sys.executable, "-m", "alembic",
            "-c", str(BACKEND_ROOT / "alembic.ini"), "upgrade", revision,
        ],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic upgrade {revision} failed (exit {result.returncode}):\n"
        + result.stderr[-4000:]
    )


def _read(database: Database, version_id: str) -> dict:
    with database.connection() as connection:
        row = connection.execute(
            "SELECT * FROM skill_versions WHERE id=?", (version_id,)
        ).fetchone()
    return {column: row[column] for column in (*IMMUTABLE, "status")}


def test_deployed_0035_frozen_trigger_contract(postgres_url, tmp_path, monkeypatch):
    owned = pg_fixtures.allocate_owned_database(str(postgres_url))
    try:
        pg_fixtures.create_owned_database(owned)
        _upgrade(owned.url, "20261019_0034")

        real_head = db_module.POSTGRES_SCHEMA_HEAD
        monkeypatch.setattr(db_module, "POSTGRES_SCHEMA_HEAD", "20261019_0034")
        before = Database(owned.url, workspace=tmp_path / "before")
        try:
            platform = SkillPlatform(before, tmp_path / "packages")
            preview = platform.preview_install(_skill_zip())
            installed = platform.confirm_install(
                preview["install_token"], granted_tools=["calculator"],
                idempotency_key="install",
            )
            version_id = installed["version_id"]
            with before.transaction() as connection:
                connection.execute(
                    "INSERT INTO skills(id,owner_id,name,status,created_at,updated_at) "
                    "VALUES ('skill_other','local-user','other-skill','INSTALLED',?,?)",
                    ("n", "n"),
                )
            original = _read(before, version_id)
        finally:
            before.close()
            monkeypatch.setattr(db_module, "POSTGRES_SCHEMA_HEAD", real_head)

        _upgrade(owned.url, "20261019_0035")
        _upgrade(owned.url, "head")
        _upgrade(owned.url, "head")

        after = Database(owned.url, workspace=tmp_path / "after")
        try:
            with after.connection() as connection:
                triggers = connection.execute(
                    "SELECT COUNT(*) FROM pg_trigger WHERE "
                    "tgrelid='skill_versions'::regclass AND "
                    "tgname='skill_versions_frozen' AND NOT tgisinternal"
                ).fetchone()[0]
                version = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0]
                rows = connection.execute(
                    "SELECT COUNT(*) FROM skill_versions"
                ).fetchone()[0]
            assert triggers == 1
            assert version == "20261019_0035"
            assert rows == 1
            assert _read(after, version_id) == original

            replacements = {
                "skill_id": "skill_other",
                "version": "9.9.9",
                "package_digest": "f" * 64,
                "manifest_digest": "e" * 64,
                "manifest_json": '{"changed":true}',
                "title": "changed title",
                "description": "changed description",
                "content": "# changed content",
                "requested_tools_json": '["other"]',
                "connectors_json": '["other"]',
                "phases_json": '["executor"]',
                "storage_path": str(tmp_path / "other-storage"),
                "created_at": "2099-01-01T00:00:00+00:00",
            }
            assert set(replacements) == set(IMMUTABLE)

            for column in IMMUTABLE:
                with pytest.raises(psycopg.errors.RaiseException) as excinfo:
                    with after.transaction() as connection:
                        connection.execute(
                            f'UPDATE skill_versions SET "{column}"=? WHERE id=?',
                            (replacements[column], version_id),
                        )
                assert excinfo.value.sqlstate == "P0001"
                assert "frozen" in str(excinfo.value)
                assert _read(after, version_id) == original

            with pytest.raises(psycopg.errors.RaiseException) as excinfo:
                with after.transaction() as connection:
                    connection.execute(
                        "DELETE FROM skill_versions WHERE id=?", (version_id,)
                    )
            assert excinfo.value.sqlstate == "P0001"
            assert "frozen" in str(excinfo.value)
            assert _read(after, version_id) == original

            with after.transaction() as connection:
                connection.execute(
                    "UPDATE skill_versions SET status='DISABLED' WHERE id=?",
                    (version_id,),
                )
            assert _read(after, version_id)["status"] == "DISABLED"

            with after.transaction() as connection:
                connection.execute(
                    "UPDATE skill_versions SET title=title WHERE id=?",
                    (version_id,),
                )
            assert _read(after, version_id) == {**original, "status": "DISABLED"}

            with pytest.raises(RuntimeError, match="rollback"):
                with after.transaction() as connection:
                    connection.execute(
                        "UPDATE skill_versions SET status='ENABLED' WHERE id=?",
                        (version_id,),
                    )
                    raise RuntimeError("rollback")
            assert _read(after, version_id)["status"] == "DISABLED"
        finally:
            after.close()
    finally:
        pg_fixtures.drop_owned_database(owned)
