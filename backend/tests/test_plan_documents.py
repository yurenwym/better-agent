import hashlib

import psycopg
import pytest

from pg02_helpers import database


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
        validate_title("   ")
    with pytest.raises(PlanDocumentValidationError):
        validate_title("x" * 121)
    with pytest.raises(PlanDocumentValidationError):
        validate_document_content("x" * (1024 * 1024 + 1))
    with pytest.raises(PlanDocumentValidationError):
        validate_document_content(" \n")


def test_database_creates_plan_document_tables_and_links(database) -> None:
    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT tablename AS name FROM pg_tables WHERE schemaname=current_schema()"
            )
        }
        assert {"plan_documents", "plan_document_versions", "plan_write_intents"} <= tables

        columns = {
            row["name"]
            for row in connection.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='turns'"
            )
        }
        assert {"artifact_kind", "artifact_operation", "artifact_title"} <= columns

        message_columns = {
            row["name"]
            for row in connection.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='thread_messages'"
            )
        }
        assert "plan_document_version_id" in message_columns

        plan_columns = {
            row["name"]
            for row in connection.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='plan_versions'"
            )
        }
        assert "source_document_version_id" in plan_columns

        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT indexname AS name FROM pg_indexes "
                "WHERE schemaname=current_schema() AND tablename='runs'"
            )
        }
        assert "uq_runs_source_turn" in indexes


def test_runs_source_turn_id_is_unique_when_present(database) -> None:
    with database.transaction() as connection:
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
        with pytest.raises(psycopg.errors.UniqueViolation):
            with connection.transaction():
                connection.execute(
                    "INSERT INTO runs(id, goal_id, session_id, state, budget_json, source_turn_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("run-2", "goal-1", "session-1", "RECEIVED", "{}", "turn-1", "now", "now"),
                )
