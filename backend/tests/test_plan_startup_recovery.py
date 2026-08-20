from __future__ import annotations


def test_build_runtime_recovers_prepared_plan_before_worker_is_available(tmp_path) -> None:
    from app.db import Database
    from app.plan_documents import content_hash

    document_id = "plan_" + "a" * 32
    version_id = "planv_" + "b" * 32
    intent_id = "intent_" + "c" * 32
    content = "# Recovered\n"
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("thread-1", "Thread", "now", "now"),
        )
        connection.execute(
            "INSERT INTO plan_documents(id, thread_id, title, file_status, created_at, updated_at) VALUES (?, ?, ?, 'pending', 'now', 'now')",
            (document_id, "thread-1", "Recovered"),
        )
        connection.execute(
            "INSERT INTO plan_document_versions(id, plan_document_id, version, title, markdown_content, content_hash, actor, status, created_at) VALUES (?, ?, 1, ?, ?, ?, 'model', 'prepared', 'now')",
            (version_id, document_id, "Recovered", content, content_hash(content)),
        )
        connection.execute(
            "INSERT INTO plan_write_intents(id, plan_document_id, version_id, target_file_hash, status, created_at) VALUES (?, ?, ?, ?, 'PREPARED', 'now')",
            (intent_id, document_id, version_id, content_hash(content)),
        )

    from app.startup import build_runtime

    runtime = build_runtime(tmp_path)

    assert runtime.plan_documents.current_version(document_id).status == "committed"
    assert runtime.plan_documents.path_for(document_id).read_text(encoding="utf-8") == content


def test_recovery_file_write_failure_marks_document_failed_and_emits_event(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore
    from app.plan_documents import PlanDocumentService, content_hash

    document_id = "plan_" + "d" * 32
    version_id = "planv_" + "e" * 32
    intent_id = "intent_" + "f" * 32
    content = "# Recovery failure\n"
    db = Database(tmp_path / "agent.db")
    events = EventStore(db)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("thread-failure", "Thread", "now", "now"),
        )
        connection.execute(
            "INSERT INTO plan_documents(id, thread_id, title, file_status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', 'now', 'now')",
            (document_id, "thread-failure", "Recovery failure"),
        )
        connection.execute(
            "INSERT INTO plan_document_versions(id, plan_document_id, version, title, markdown_content, content_hash, actor, status, created_at) "
            "VALUES (?, ?, 1, ?, ?, ?, 'model', 'prepared', 'now')",
            (version_id, document_id, "Recovery failure", content, content_hash(content)),
        )
        connection.execute(
            "INSERT INTO plan_write_intents(id, plan_document_id, version_id, expected_file_hash, target_file_hash, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'PREPARED', 'now')",
            (intent_id, document_id, version_id, None, content_hash(content)),
        )

    class FailingProjector:
        def path_for(self, document_id: str):
            return tmp_path / "missing-plan.md"

        def read_hash(self, document_id: str):
            return None

        def project(self, *args, **kwargs):
            raise OSError("C:/private/server/path/plan.md is unavailable")

    service = PlanDocumentService(
        db,
        tmp_path / "data",
        projector=FailingProjector(),
        events=events,
    )
    service.recover_pending_intents()

    document = service.get_document(document_id)
    assert document.file_status == "failed"
    intent = service.pending_intents(document_id)[0]
    assert intent.status == "FAILED"
    failures = [event for event in events.list("thread-failure") if event.type == "plan.document_failed"]
    assert len(failures) == 1
    assert failures[0].data["reason"] == "plan file unavailable"
    assert "private/server/path" not in str(failures[0].data)
