import pytest


def _service(tmp_path, projector=None):
    from app.db import Database
    from app.plan_documents import PlanDocumentService

    return PlanDocumentService(
        Database(tmp_path / "agent.db"),
        tmp_path / "data",
        projector=projector,
    )


def test_first_model_revision_commits_database_and_file(tmp_path) -> None:
    service = _service(tmp_path)

    version = service.save_model_revision(
        thread_id="thread-1",
        title="旅行计划",
        markdown_content="# 旅行计划\n\n第一天\n",
        source_turn_id="turn-1",
        source_message_id="message-1",
        actor="model",
    )

    document = service.get_by_thread("thread-1")
    assert document.current_version_id == version.id
    assert version.version == 1
    assert version.status == "committed"
    assert service.path_for(document.id).read_text(encoding="utf-8") == "# 旅行计划\n\n第一天\n"


def test_same_source_turn_returns_existing_committed_revision(tmp_path) -> None:
    service = _service(tmp_path)
    kwargs = {
        "thread_id": "thread-1",
        "title": "计划",
        "markdown_content": "# 计划\n",
        "source_turn_id": "turn-1",
        "source_message_id": "message-1",
        "actor": "model",
    }

    first = service.save_model_revision(**kwargs)
    retry = service.save_model_revision(**{**kwargs, "source_message_id": "message-retry"})

    assert retry.id == first.id
    assert service.list_versions(first.plan_document_id)[-1].version == 1


def test_expected_version_or_hash_conflict_preserves_current_head(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.save_model_revision(
        thread_id="thread-1",
        title="计划",
        markdown_content="# v1\n",
        source_turn_id="turn-1",
        source_message_id="message-1",
        actor="model",
    )

    with pytest.raises(ValueError, match="plan document head conflict"):
        service.save_model_revision(
            thread_id="thread-1",
            title="计划",
            markdown_content="# stale\n",
            source_turn_id="turn-2",
            source_message_id="message-2",
            actor="user",
            expected_version_id="missing",
            expected_file_hash=first.content_hash,
        )

    current = service.current_version(first.plan_document_id)
    assert current.id == first.id
    assert current.markdown_content == "# v1\n"


def test_restore_creates_new_version_without_deleting_history(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.save_model_revision(
        thread_id="thread-1",
        title="计划",
        markdown_content="# v1\n",
        source_turn_id="turn-1",
        source_message_id="message-1",
        actor="model",
    )
    second = service.save_model_revision(
        thread_id="thread-1",
        title="计划",
        markdown_content="# v2\n",
        source_turn_id="turn-2",
        source_message_id="message-2",
        actor="user",
        expected_version_id=first.id,
        expected_file_hash=first.content_hash,
    )

    restored = service.restore_version(second.plan_document_id, first.version, actor="restore")

    assert restored.version == 3
    assert restored.markdown_content == "# v1\n"
    assert [item.version for item in service.list_versions(second.plan_document_id)] == [1, 2, 3]


def test_write_failure_leaves_retryable_intent_and_no_committed_head(tmp_path) -> None:
    class FailingProjector:
        def project(self, *args, **kwargs):
            raise OSError("disk unavailable")

    service = _service(tmp_path, projector=FailingProjector())

    with pytest.raises(OSError, match="disk unavailable"):
        service.save_model_revision(
            thread_id="thread-1",
            title="计划",
            markdown_content="# 计划\n",
            source_turn_id="turn-1",
            source_message_id="message-1",
            actor="model",
        )

    document = service.get_by_thread("thread-1")
    assert document.current_version_id is None
    assert service.pending_intents(document.id)[0].status == "FAILED"
