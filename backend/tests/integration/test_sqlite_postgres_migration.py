from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.conversation import ConversationService
from app.db import Database
from app.memory_v2 import MemoryStore
from scripts.migrate_sqlite_to_postgres import migrate


def _populated_sqlite(tmp_path: Path) -> tuple[Path, str, str]:
    sqlite_path = tmp_path / "populated-agent.db"
    db = Database(sqlite_path)
    conversation = ConversationService(db)
    thread = conversation.create_thread("迁移验证")
    submission = conversation.accept_turn(
        thread.id,
        client_turn_id="migration-turn-1",
        content='请保留 Unicode 与 JSON：你好 {"answer": 42}',
        skill_names=[],
    )
    with db.transaction() as connection:
        now = "2026-09-06T00:00:00+00:00"
        connection.execute(
            "UPDATE turns SET status='COMPLETED',policy='answer',content_shape='general',"
            "reason_code='content_only',updated_at=? WHERE id=?",
            (now, submission.turn_id),
        )
        connection.execute(
            "UPDATE turn_jobs SET status='COMPLETED',finished_at=? WHERE turn_id=?",
            (now, submission.turn_id),
        )
        connection.execute(
            "INSERT INTO thread_messages("
            "id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at,completed_at"
            ") VALUES (?,?,?,?,?,'ready',1,?,2,?,?)",
            (
                "migration-assistant-message",
                thread.id,
                submission.turn_id,
                "assistant",
                "已保留结构化内容。",
                len("已保留结构化内容。"),
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE threads SET active_turn_id=NULL,updated_at=? WHERE id=?",
            (now, thread.id),
        )
    memory = MemoryStore(db, tmp_path / "memory").remember(
        owner_id="local-user",
        kind="preference",
        scope_type="user",
        scope_id="",
        content="偏好简洁、可验证的回答",
        idempotency_key="migration-memory-1",
    )
    db.close()
    return sqlite_path, submission.turn_id, memory.id


def _source_counts(sqlite_path: Path, tables: set[str]) -> dict[str, int]:
    with sqlite3.connect(sqlite_path) as connection:
        return {
            table: connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in tables
        }


def test_migrate_populated_sqlite_emits_verified_count_and_hash_manifest(
    migrated_postgres_url, tmp_path
) -> None:
    sqlite_path, turn_id, memory_id = _populated_sqlite(tmp_path)

    manifest = migrate(sqlite_path, migrated_postgres_url)

    assert {table: item.count for table, item in manifest.items()} == _source_counts(
        sqlite_path, set(manifest)
    )
    assert all(
        len(item.sha256) == 64
        and set(item.sha256) <= set("0123456789abcdef")
        for item in manifest.values()
    )
    assert manifest["threads"].count == 1
    assert manifest["turns"].count == 1
    assert manifest["thread_messages"].count == 2
    assert manifest["memory_entries"].count == 1
    assert manifest["memory_revisions"].count == 1

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        message = connection.execute(
            "SELECT content FROM thread_messages WHERE turn_id=%s", (turn_id,)
        ).fetchone()
        memory = connection.execute(
            "SELECT r.content FROM memory_entries e JOIN memory_revisions r "
            "ON r.id=e.current_revision_id WHERE e.id=%s",
            (memory_id,),
        ).fetchone()
    assert message["content"] == '请保留 Unicode 与 JSON：你好 {"answer": 42}'
    assert memory["content"] == "偏好简洁、可验证的回答"


def test_migrate_rejects_nonempty_target_without_changing_existing_rows(
    migrated_postgres_url, tmp_path
) -> None:
    sqlite_path, _, _ = _populated_sqlite(tmp_path)
    first_manifest = migrate(sqlite_path, migrated_postgres_url)

    with pytest.raises(RuntimeError, match="target must be empty before import"):
        migrate(sqlite_path, migrated_postgres_url)

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        actual_counts = {
            table: connection.execute(
                sql.SQL("SELECT COUNT(*) AS count FROM {}").format(sql.Identifier(table))
            ).fetchone()["count"]
            for table in first_manifest
        }
    assert actual_counts == {
        table: item.count for table, item in first_manifest.items()
    }


def test_migrate_rejects_sqlite_foreign_key_orphans_before_target_writes(
    migrated_postgres_url, tmp_path
) -> None:
    sqlite_path, _, _ = _populated_sqlite(tmp_path)
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO memory_revisions("
            "id,entry_id,revision_no,operation,content,content_hash,actor,source_refs_json,created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "orphan-revision",
                "missing-memory-entry",
                1,
                "CREATE",
                "orphan",
                "orphan-hash",
                "test",
                "[]",
                "2026-09-06T00:00:00+00:00",
            ),
        )

    with pytest.raises(
        RuntimeError,
        match=r"foreign key orphan.*memory_revisions.*memory_entries.*fk_index=0",
    ):
        migrate(sqlite_path, migrated_postgres_url)

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM threads").fetchone()[
            "count"
        ] == 0
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM memory_revisions"
        ).fetchone()["count"] == 0


def test_migrate_rebuilds_memory_search_and_embedding_outbox(
    migrated_postgres_url, tmp_path
) -> None:
    sqlite_path, _, memory_id = _populated_sqlite(tmp_path)

    migrate(sqlite_path, migrated_postgres_url)

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        entry = connection.execute(
            "SELECT owner_id,current_revision_id FROM memory_entries WHERE id=%s",
            (memory_id,),
        ).fetchone()
        revision = connection.execute(
            "SELECT content,content_hash FROM memory_revisions WHERE id=%s",
            (entry["current_revision_id"],),
        ).fetchone()
        search = connection.execute(
            "SELECT owner_id,content FROM memory_fts WHERE entry_id=%s",
            (memory_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT profile_id,content_hash,status FROM embedding_jobs WHERE revision_id=%s",
            (entry["current_revision_id"],),
        ).fetchone()

    assert search == {
        "owner_id": entry["owner_id"],
        "content": revision["content"],
    }
    assert job == {
        "profile_id": "siliconflow-qwen3-embedding-4b-1024",
        "content_hash": revision["content_hash"],
        "status": "QUEUED",
    }


def test_migrate_advances_postgres_identity_sequences(migrated_postgres_url, tmp_path) -> None:
    sqlite_path, _, _ = _populated_sqlite(tmp_path)

    migrate(sqlite_path, migrated_postgres_url)

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        maximum = connection.execute(
            "SELECT MAX(row_id) AS maximum FROM thread_events"
        ).fetchone()["maximum"]
        generated = connection.execute(
            "SELECT nextval(pg_get_serial_sequence('thread_events','row_id')) AS generated"
        ).fetchone()["generated"]

    assert maximum is not None
    assert generated == maximum + 1


def test_migrate_rejects_source_columns_missing_from_target_before_writes(
    migrated_postgres_url, tmp_path
) -> None:
    sqlite_path, _, _ = _populated_sqlite(tmp_path)
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute("ALTER TABLE threads ADD COLUMN sqlite_only_note TEXT")

    with pytest.raises(
        RuntimeError,
        match=r"target schema is missing source columns.*threads.*sqlite_only_note",
    ):
        migrate(sqlite_path, migrated_postgres_url)

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM threads").fetchone()[
            "count"
        ] == 0
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM sqlite_import_completions"
        ).fetchone()["count"] == 0


def test_migrate_records_completion_only_after_verified_import(
    migrated_postgres_url, tmp_path
) -> None:
    sqlite_path, _, _ = _populated_sqlite(tmp_path)

    manifest = migrate(sqlite_path, migrated_postgres_url)

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        marker = connection.execute(
            "SELECT source_manifest_sha256,manifest_json,completed_at "
            "FROM sqlite_import_completions WHERE id=1"
        ).fetchone()

    assert marker is not None
    assert len(marker["source_manifest_sha256"]) == 64
    assert marker["manifest_json"] == {
        table: {"count": item.count, "sha256": item.sha256}
        for table, item in manifest.items()
    }
    assert marker["completed_at"] is not None


def test_migrate_rolls_back_data_and_completion_marker_when_rebuild_fails(
    migrated_postgres_url, tmp_path
) -> None:
    sqlite_path, _, _ = _populated_sqlite(tmp_path)
    with psycopg.connect(migrated_postgres_url) as connection:
        connection.execute("ALTER TABLE memory_fts RENAME TO unavailable_memory_fts")
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            migrate(sqlite_path, migrated_postgres_url)
    finally:
        with psycopg.connect(migrated_postgres_url) as connection:
            connection.execute(
                "ALTER TABLE unavailable_memory_fts RENAME TO memory_fts"
            )

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM threads").fetchone()[
            "count"
        ] == 0
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM sqlite_import_completions"
        ).fetchone()["count"] == 0


def test_migrate_requires_active_embedding_profile_for_memory_backfill(
    migrated_postgres_url, tmp_path
) -> None:
    sqlite_path, _, _ = _populated_sqlite(tmp_path)
    with psycopg.connect(migrated_postgres_url) as connection:
        connection.execute("UPDATE embedding_profiles SET active=false")
    try:
        with pytest.raises(RuntimeError, match="active embedding profile"):
            migrate(sqlite_path, migrated_postgres_url)
    finally:
        with psycopg.connect(migrated_postgres_url) as connection:
            connection.execute("UPDATE embedding_profiles SET active=true")

    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        assert connection.execute("SELECT COUNT(*) AS count FROM threads").fetchone()[
            "count"
        ] == 0
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM sqlite_import_completions"
        ).fetchone()["count"] == 0


def test_fresh_postgres_install_does_not_require_sqlite_import_marker(
    migrated_postgres_url, tmp_path
) -> None:
    with psycopg.connect(migrated_postgres_url, row_factory=dict_row) as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM sqlite_import_completions"
        ).fetchone()["count"] == 0

    db = Database(migrated_postgres_url, workspace=tmp_path / "fresh-workspace")
    db.close()
