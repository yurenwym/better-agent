from __future__ import annotations

from pathlib import Path

import pytest


def _service(tmp_path):
    from app.db import Database
    from app.plan_documents import PlanDocumentService

    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("thread-1", "Thread", "now", "now"),
        )
    return db, PlanDocumentService(db, tmp_path)


def test_external_file_change_during_import_is_rejected_without_new_revision(tmp_path, monkeypatch) -> None:
    db, service = _service(tmp_path)
    first = service.save_model_revision(
        thread_id="thread-1",
        title="Plan",
        markdown_content="# Original\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    path = service.path_for(first.plan_document_id)
    path.write_text("# External\n", encoding="utf-8", newline="\n")

    original_open = Path.open
    mutated = False

    class MutatingReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, *args, **kwargs):
            nonlocal mutated
            content = self.handle.read(*args, **kwargs)
            if not mutated:
                mutated = True
                path.write_text("# Changed while importing\n", encoding="utf-8", newline="\n")
            return content

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def mutate_during_read(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        if self == path and str(args[0] if args else kwargs.get("mode", "r")).startswith("r"):
            return MutatingReader(handle)
        return handle

    monkeypatch.setattr(Path, "open", mutate_during_read)

    from app.plan_documents import PlanDocumentValidationError

    with pytest.raises(PlanDocumentValidationError, match="changed while reading"):
        service.sync_file(first.plan_document_id)

    assert [version.version for version in service.list_versions(first.plan_document_id)] == [1]
    assert service.get_document(first.plan_document_id).file_status == "failed"
    assert path.read_text(encoding="utf-8") == "# Changed while importing\n"


def test_database_and_file_conflict_emits_one_semantic_conflict_event(tmp_path) -> None:
    from app.events import ThreadEventStore
    from app.plan_documents import PlanDocumentConflict

    db, service = _service(tmp_path)
    service.events = ThreadEventStore(db)
    first = service.save_model_revision(
        thread_id="thread-1",
        title="Plan",
        markdown_content="# v1\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    second = service.save_model_revision(
        thread_id="thread-1",
        title="Plan",
        markdown_content="# v2\n",
        source_turn_id=None,
        source_message_id=None,
        actor="user",
        expected_version_id=first.id,
        expected_file_hash=first.content_hash,
    )
    path = service.path_for(second.plan_document_id)
    path.write_text(first.markdown_content, encoding="utf-8", newline="\n")
    with db.durable_transaction() as connection:
        connection.execute(
            "UPDATE plan_documents SET projected_version_id = ? WHERE id = ?",
            (first.id, second.plan_document_id),
        )

    with pytest.raises(PlanDocumentConflict):
        service.sync_file(second.plan_document_id)
    with pytest.raises(PlanDocumentConflict):
        service.sync_file(second.plan_document_id)

    conflicts = [event for event in service.events.list("thread-1") if event.type == "plan.document_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0].data["version"] == 2
    assert "markdown" not in conflicts[0].data
