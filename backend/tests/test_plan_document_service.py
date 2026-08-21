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


def test_model_revision_links_its_source_message_during_finalize(tmp_path) -> None:
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, object())
    thread = runtime.conversation.create_thread("Chat")
    accepted = runtime.conversation.accept_turn(thread.id, "client-plan-message-link", "Create a plan", [])
    source_message = runtime.conversation.messages(thread.id)[0]

    version = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="计划",
        markdown_content="# 计划\n",
        source_turn_id=accepted.turn_id,
        source_message_id=source_message.id,
        actor="model",
    )

    assert runtime.conversation.messages(thread.id)[0].plan_document_version_id == version.id


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


def test_deleted_document_can_be_reused_by_a_new_model_revision(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.save_model_revision(
        thread_id="thread-1",
        title="旧计划",
        markdown_content="# v1\n",
        source_turn_id="turn-1",
        source_message_id="message-1",
        actor="model",
    )
    service.delete_document(
        first.plan_document_id,
        expected_version=first.version,
        expected_file_hash=first.content_hash,
    )

    revived = service.save_model_revision(
        thread_id="thread-1",
        title="新计划",
        markdown_content="# v2\n",
        source_turn_id="turn-2",
        source_message_id="message-2",
        actor="model",
    )

    document = service.get_by_thread("thread-1")
    assert revived.version == 2
    assert document.deleted_at is None
    assert service.current_version(document.id).id == revived.id
    assert service.path_for(document.id).read_text(encoding="utf-8") == "# v2\n"


def test_delete_tombstone_survives_file_cleanup_failure_and_restart_recovery(tmp_path) -> None:
    service = _service(tmp_path)
    first = service.save_model_revision(
        thread_id="thread-1",
        title="计划",
        markdown_content="# v1\n",
        source_turn_id="turn-1",
        source_message_id="message-1",
        actor="model",
    )
    path = service.path_for(first.plan_document_id)
    real_remove = service.projector.remove

    def fail_remove(*args, **kwargs):
        raise OSError("temporary cleanup failure")

    service.projector.remove = fail_remove
    service.delete_document(
        first.plan_document_id,
        expected_version=first.version,
        expected_file_hash=first.content_hash,
    )

    with pytest.raises(KeyError):
        service.get_document(first.plan_document_id)
    assert path.exists()

    service.projector.remove = real_remove
    service.recover_pending_intents()

    assert not path.exists()


def test_restore_rejects_a_prepared_revision(tmp_path) -> None:
    from app.plan_files import PlanFileProjector

    service = _service(tmp_path)
    first = service.save_model_revision(
        thread_id="thread-1",
        title="计划",
        markdown_content="# v1\n",
        source_turn_id="turn-1",
        source_message_id="message-1",
        actor="model",
    )

    class FailingProjector:
        def read_hash(self, *args, **kwargs):
            return first.content_hash

        def project(self, *args, **kwargs):
            raise OSError("temporary disk failure")

    service.projector = FailingProjector()
    with pytest.raises(OSError, match="temporary disk failure"):
        service.save_model_revision(
            thread_id="thread-1",
            title="计划",
            markdown_content="# prepared\n",
            source_turn_id="turn-2",
            source_message_id="message-2",
            actor="model",
            expected_version_id=first.id,
            expected_file_hash=first.content_hash,
        )
    prepared = service.list_versions(first.plan_document_id)[-1]
    assert prepared.status == "prepared"

    service.projector = PlanFileProjector(tmp_path / "data")
    with pytest.raises(ValueError, match="committed"):
        service.restore_version(
            first.plan_document_id,
            prepared.version,
            expected_version=first.version,
            expected_file_hash=first.content_hash,
        )


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


def test_same_source_turn_retry_recovers_prepared_revision_instead_of_returning_it(tmp_path) -> None:
    from app.plan_files import PlanFileProjector

    class FailingOnceProjector(PlanFileProjector):
        def __init__(self, root):
            super().__init__(root)
            self.failed = False

        def project(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise OSError("temporary disk failure")
            return super().project(*args, **kwargs)

    projector = FailingOnceProjector(tmp_path / "data")
    service = _service(tmp_path, projector=projector)
    kwargs = {
        "thread_id": "thread-1",
        "title": "计划",
        "markdown_content": "# 计划\n",
        "source_turn_id": "turn-retry",
        "source_message_id": "message-retry",
        "actor": "model",
    }

    with pytest.raises(OSError, match="temporary disk failure"):
        service.save_model_revision(**kwargs)

    recovered = service.save_model_revision(**kwargs)

    assert recovered.status == "committed"
    assert service.list_versions(recovered.plan_document_id)[0].status == "committed"
    assert service.path_for(recovered.plan_document_id).read_text(encoding="utf-8") == "# 计划\n"


def test_revision_owner_check_can_fence_after_file_projection_before_finalize(tmp_path) -> None:
    service = _service(tmp_path)
    checks = 0

    def owner_check() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("turn lease lost")

    with pytest.raises(RuntimeError, match="turn lease lost"):
        service.save_model_revision(
            thread_id="thread-1",
            title="璁″垝",
            markdown_content="# 璁″垝\n",
            source_turn_id="turn-owner-check",
            source_message_id="message-owner-check",
            actor="model",
            owner_check=owner_check,
        )

    document = service.get_by_thread("thread-1")
    assert checks == 2
    assert service.list_versions(document.id)[0].status == "prepared"
    assert service.path_for(document.id).read_text(encoding="utf-8") == "# 璁″垝\n"
