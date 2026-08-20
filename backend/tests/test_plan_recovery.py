import hashlib
import json

import pytest


def _prepared_service(tmp_path, *, content="# v1\n", expected_hash=None):
    from app.db import Database
    from app.plan_documents import PlanDocumentService, content_hash

    db = Database(tmp_path / "agent.db")
    service = PlanDocumentService(db, tmp_path / "data")
    document_id = "plan_" + "c" * 32
    version_id = "planv_" + "d" * 32
    intent_id = "intent_" + "e" * 32
    target_hash = content_hash(content)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO plan_documents(id, thread_id, title, file_status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', 'now', 'now')",
            (document_id, "thread-1", "计划"),
        )
        connection.execute(
            "INSERT INTO plan_document_versions(" 
            "id, plan_document_id, version, title, markdown_content, content_hash, actor, status, created_at) "
            "VALUES (?, ?, 1, ?, ?, ?, 'model', 'prepared', 'now')",
            (version_id, document_id, "计划", content, target_hash),
        )
        connection.execute(
            "INSERT INTO plan_write_intents(" 
            "id, plan_document_id, version_id, expected_file_hash, target_file_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'PREPARED', 'now')",
            (intent_id, document_id, version_id, expected_hash, target_hash),
        )
    return service, document_id, version_id, content


def test_recover_prepared_intent_projects_and_commits(tmp_path) -> None:
    service, document_id, version_id, content = _prepared_service(tmp_path)

    service.recover_pending_intents()

    assert service.get_document(document_id).current_version_id == version_id
    assert service.get_version(version_id).status == "committed"
    assert service.path_for(document_id).read_text(encoding="utf-8") == content


def test_recover_target_hash_finalizes_without_requiring_model(tmp_path) -> None:
    service, document_id, version_id, content = _prepared_service(tmp_path)
    service.projector.project(document_id, content)

    service.recover_pending_intents()

    assert service.get_version(version_id).status == "committed"
    assert service.get_document(document_id).file_status == "ready"


def test_recover_third_hash_marks_conflict_without_overwriting_file(tmp_path) -> None:
    service, document_id, version_id, _ = _prepared_service(tmp_path)
    service.projector.project(document_id, "external\n")

    service.recover_pending_intents()

    assert service.get_version(version_id).status == "prepared"
    assert service.get_document(document_id).file_status == "conflict"
    assert service.path_for(document_id).read_text(encoding="utf-8") == "external\n"


def test_recover_committed_missing_file_rebuilds_from_sqlite(tmp_path) -> None:
    service, document_id, version_id, content = _prepared_service(tmp_path)
    service.recover_pending_intents()
    service.path_for(document_id).unlink()

    service.recover_pending_intents()

    assert service.get_version(version_id).status == "committed"
    assert service.path_for(document_id).read_text(encoding="utf-8") == content
