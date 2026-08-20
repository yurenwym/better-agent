import hashlib
import sqlite3

import pytest


def test_normalize_markdown_preserves_content_except_line_endings_and_rejects_nul() -> None:
    from app.plan_documents import PlanDocumentValidationError, normalize_markdown

    assert normalize_markdown("a\r\nb\rb\n") == "a\nb\nb\n"
    assert normalize_markdown("trailing  \n") == "trailing  \n"
    with pytest.raises(PlanDocumentValidationError):
        normalize_markdown("bad\x00content")


def test_content_hash_is_sha256_of_bom_free_utf8() -> None:
    from app.plan_documents import content_hash

    content = "# 计划\n"
    assert content_hash(content) == "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_plan_document_limits_title_and_markdown_size() -> None:
    from app.plan_documents import PlanDocumentValidationError, validate_document_content, validate_title

    assert validate_title("旅行计划") == "旅行计划"
    with pytest.raises(PlanDocumentValidationError):
        validate_title("")
    with pytest.raises(PlanDocumentValidationError):
        validate_title("x" * 121)
    with pytest.raises(PlanDocumentValidationError):
        validate_document_content("x" * (1024 * 1024 + 1))


def test_database_creates_plan_document_tables_and_links(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"plan_documents", "plan_document_versions", "plan_write_intents"} <= tables

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(turns)")
        }
        assert {"artifact_kind", "artifact_operation", "artifact_title"} <= columns

        message_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(thread_messages)")
        }
        assert "plan_document_version_id" in message_columns

        plan_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(plan_versions)")
        }
        assert "source_document_version_id" in plan_columns

        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(runs)")
        }
        assert "uq_runs_source_turn" in indexes


def test_runs_source_turn_id_is_unique_when_present(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO goals(id, title, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("goal-1", "Goal", "", "now", "now"),
        )
        connection.execute(
            "INSERT INTO sessions(id, goal_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("session-1", "goal-1", "now", "now"),
        )
        connection.execute(
            "INSERT INTO runs(id, goal_id, session_id, state, budget_json, source_turn_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("run-1", "goal-1", "session-1", "RECEIVED", "{}", "turn-1", "now", "now"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runs(id, goal_id, session_id, state, budget_json, source_turn_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("run-2", "goal-1", "session-1", "RECEIVED", "{}", "turn-1", "now", "now"),
            )
