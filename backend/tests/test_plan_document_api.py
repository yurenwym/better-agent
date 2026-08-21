from __future__ import annotations

from fastapi.testclient import TestClient

from test_runtime import make_runtime


def _headers(app):
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
    }


def _seed(tmp_path):
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    thread = runtime.conversation.create_thread("Plan thread")
    first = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Travel plan",
        markdown_content="# Version 1\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )
    app = create_app(runtime=runtime)
    return runtime, app, TestClient(app), thread, first


def test_plan_document_read_routes_restore_refresh_state_without_run(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    headers = _headers(app)

    thread_plan = client.get(f"/api/threads/{thread.id}/plan", headers=headers)
    snapshot = client.get(f"/api/plans/{document.id}", headers=headers)
    versions = client.get(f"/api/plans/{document.id}/versions", headers=headers)
    version = client.get(f"/api/plans/{document.id}/versions/{first.version}", headers=headers)
    file_response = client.get(f"/api/plans/{document.id}/file", headers=headers)

    assert thread_plan.status_code == snapshot.status_code == versions.status_code == version.status_code == 200
    assert thread_plan.json()["plan"]["current"]["version"] == 1
    assert snapshot.json()["current"]["markdown"] == "# Version 1\n"
    assert [item["version"] for item in versions.json()["versions"]] == [1]
    assert "markdown" not in versions.json()["versions"][0]
    assert version.json()["markdown"] == "# Version 1\n"
    assert file_response.text == "# Version 1\n"
    assert file_response.headers["content-type"].startswith("text/markdown")
    assert all(count == 0 for count in (
        _count(runtime, "goals"),
        _count(runtime, "runs"),
        _count(runtime, "plan_versions"),
    ))


def test_plan_document_index_lists_saved_titles_without_markdown(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    second_thread = runtime.conversation.create_thread("Second plan thread")
    second = runtime.plan_documents.save_model_revision(
        thread_id=second_thread.id,
        title="Training plan",
        markdown_content="# Training\n",
        source_turn_id=None,
        source_message_id=None,
        actor="model",
    )

    response = client.get("/api/plans", headers=_headers(app))

    assert response.status_code == 200
    plans = response.json()["plans"]
    assert {item["id"] for item in plans} == {first.plan_document_id, second.plan_document_id}
    assert {item["title"] for item in plans} == {"Travel plan", "Training plan"}
    assert all("markdown" not in item for item in plans)

    document = runtime.plan_documents.get_by_thread(thread.id)
    current = runtime.plan_documents.current_version(document.id)
    runtime.plan_documents.delete_document(
        document.id,
        expected_version=current.version,
        expected_file_hash=current.content_hash,
    )

    remaining = client.get("/api/plans", headers=_headers(app))
    assert [item["id"] for item in remaining.json()["plans"]] == [second.plan_document_id]


def test_plan_history_routes_are_paged_and_return_metadata_only(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    current = first
    for index in range(2, 5):
        current = runtime.plan_documents.save_model_revision(
            thread_id=thread.id,
            title="Travel plan",
            markdown_content=f"# Version {index}\n",
            source_turn_id=None,
            source_message_id=None,
            actor="user",
            expected_version_id=current.id,
            expected_file_hash=current.content_hash,
        )

    headers = _headers(app)
    versions = client.get(f"/api/plans/{document.id}/versions?limit=2&offset=1", headers=headers)
    snapshot = client.get(f"/api/plans/{document.id}?limit=2&offset=2", headers=headers)

    assert versions.status_code == snapshot.status_code == 200
    assert [item["version"] for item in versions.json()["versions"]] == [2, 3]
    assert versions.json()["versions_total"] == 4
    assert versions.json()["versions_has_more"] is True
    assert all("markdown" not in item for item in versions.json()["versions"])
    assert [item["version"] for item in snapshot.json()["versions"]] == [3, 4]


def test_plan_document_put_uses_version_hash_cas_and_preserves_conflict_metadata(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    headers = _headers(app)

    saved = client.put(
        f"/api/plans/{document.id}",
        headers=headers,
        json={
            "expected_version": first.version,
            "expected_content_hash": first.content_hash,
            "title": "Travel plan updated",
            "markdown": "# Version 2\n\nUpdated",
            "change_summary": "add detail",
        },
    )
    stale = client.put(
        f"/api/plans/{document.id}",
        headers=headers,
        json={
            "expected_version": first.version,
            "expected_content_hash": first.content_hash,
            "title": "Stale",
            "markdown": "# stale\n",
        },
    )

    assert saved.status_code == 200
    assert saved.json()["version"] == 2
    assert stale.status_code == 409
    assert stale.json()["current"]["version"] == 2
    assert runtime.plan_documents.current_version(document.id).markdown_content == "# Version 2\n\nUpdated"


def test_plan_document_delete_requires_cas_and_removes_document_without_erasing_history(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    headers = _headers(app)
    path = runtime.plan_documents.path_for(document.id)

    response = client.request(
        "DELETE",
        f"/api/plans/{document.id}",
        headers=headers,
        json={
            "expected_version": first.version,
            "expected_content_hash": first.content_hash,
        },
    )

    assert response.status_code == 204
    assert not path.exists()
    assert client.get(f"/api/plans/{document.id}", headers=headers).status_code == 404
    assert client.get(f"/api/threads/{thread.id}/plan", headers=headers).json() == {"plan": None}
    with runtime.db.connection() as connection:
        assert connection.execute(
            "SELECT status FROM plan_document_versions WHERE id = ?", (first.id,)
        ).fetchone()["status"] == "committed"
        assert connection.execute(
            "SELECT deleted_at FROM plan_documents WHERE id = ?", (document.id,)
        ).fetchone()["deleted_at"] is not None


def test_plan_document_delete_rejects_a_stale_head_and_keeps_the_file(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    headers = _headers(app)
    second = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="Travel plan",
        markdown_content="# Version 2\n",
        source_turn_id=None,
        source_message_id=None,
        actor="user",
        expected_version_id=first.id,
        expected_file_hash=first.content_hash,
    )
    path = runtime.plan_documents.path_for(document.id)

    response = client.request(
        "DELETE",
        f"/api/plans/{document.id}",
        headers=headers,
        json={
            "expected_version": first.version,
            "expected_content_hash": first.content_hash,
        },
    )

    assert response.status_code == 409
    assert response.json()["current"]["version"] == second.version
    assert path.exists()
    assert runtime.plan_documents.get_document(document.id).current_version_id == second.id


def test_plan_document_delete_maps_an_external_file_change_to_conflict(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    path = runtime.plan_documents.path_for(document.id)
    path.write_text("# external\n", encoding="utf-8", newline="\n")

    response = client.request(
        "DELETE",
        f"/api/plans/{document.id}",
        headers=_headers(app),
        json={
            "expected_version": first.version,
            "expected_content_hash": first.content_hash,
        },
    )

    assert response.status_code == 409
    assert runtime.plan_documents.get_document(document.id).file_status == "ready"
    assert path.read_text(encoding="utf-8") == "# external\n"


def test_plan_projection_failure_returns_retryable_metadata_instead_of_500(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)

    class FailingProjector:
        def read_hash(self, document_id):
            return first.content_hash

        def project(self, *args, **kwargs):
            raise OSError("disk unavailable")

    runtime.plan_documents.projector = FailingProjector()
    response = client.put(
        f"/api/plans/{document.id}",
        headers=_headers(app),
        json={
            "expected_version": first.version,
            "expected_content_hash": first.content_hash,
            "title": "Travel plan",
            "markdown": "# Version 2\n",
        },
    )

    assert response.status_code == 503
    assert response.json()["retry"] is True
    assert response.json()["current"]["file_status"] == "failed"


def test_restore_and_sync_projection_failures_are_retryable(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    path = runtime.plan_documents.path_for(document.id)
    real_projector = runtime.plan_documents.projector

    class FailingProjector:
        def path_for(self, document_id):
            return real_projector.path_for(document_id)

        def read_hash(self, document_id):
            return first.content_hash

        def read_text_stable(self, document_id):
            raise OSError("disk unavailable")

        def project(self, *args, **kwargs):
            raise OSError("disk unavailable")

    runtime.plan_documents.projector = FailingProjector()
    restored = client.post(
        f"/api/plans/{document.id}/restore",
        headers=_headers(app),
        json={"version": 1, "expected_version": 1, "expected_content_hash": first.content_hash},
    )
    read_failed = client.post(f"/api/plans/{document.id}/sync-file", headers=_headers(app), json={})
    path.unlink()
    write_failed = client.post(f"/api/plans/{document.id}/sync-file", headers=_headers(app), json={})

    assert restored.status_code == read_failed.status_code == write_failed.status_code == 503
    assert restored.json()["retry"] is True
    assert read_failed.json()["retry"] is True
    assert write_failed.json()["retry"] is True


def test_plan_document_restore_sync_and_retry_do_not_call_model(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    headers = _headers(app)
    second = client.put(
        f"/api/plans/{document.id}",
        headers=headers,
        json={
            "expected_version": 1,
            "expected_content_hash": first.content_hash,
            "title": "Travel plan",
            "markdown": "# Version 2\n",
        },
    )

    restored = client.post(
        f"/api/plans/{document.id}/restore",
        headers=headers,
        json={"version": 1, "expected_version": 2, "expected_content_hash": second.json()["content_hash"]},
    )
    document_path = runtime.plan_documents.path_for(document.id)
    document_path.write_text("# external\n", encoding="utf-8", newline="\n")
    synced = client.post(f"/api/plans/{document.id}/sync-file", headers=headers, json={})
    retried = client.post(f"/api/plans/{document.id}/retry-projection", headers=headers, json={})

    assert restored.status_code == synced.status_code == retried.status_code == 200
    assert restored.json()["version"] == 3
    assert synced.json()["version"] == 4
    assert retried.json()["file_status"] == "ready"
    assert _count(runtime, "runs") == 0


def test_plan_document_write_requires_local_csrf(tmp_path) -> None:
    _, app, client, thread, _ = _seed(tmp_path)
    response = client.put(
        f"/api/plans/{thread.id}",
        headers={"host": "127.0.0.1:8000", "content-type": "application/json"},
        json={},
    )

    assert response.status_code == 403


def test_plan_file_read_does_not_repair_missing_projection_without_a_write_request(tmp_path) -> None:
    runtime, app, client, thread, _ = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    path = runtime.plan_documents.path_for(document.id)
    path.unlink()

    response = client.get(f"/api/plans/{document.id}/file", headers=_headers(app))

    assert response.status_code == 409
    assert not path.exists()


def test_plan_file_read_rejects_an_oversized_or_unstable_file(tmp_path) -> None:
    runtime, app, client, thread, _ = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    path = runtime.plan_documents.path_for(document.id)
    path.write_bytes(b"x" * (1024 * 1024 + 1))

    response = client.get(f"/api/plans/{document.id}/file", headers=_headers(app))

    assert response.status_code == 409


def test_plan_file_read_maps_filesystem_errors_to_a_stable_message(tmp_path) -> None:
    runtime, app, client, thread, _ = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    real_projector = runtime.plan_documents.projector

    class FailingProjector:
        def path_for(self, document_id):
            return real_projector.path_for(document_id)

        def read_text_stable(self, document_id):
            raise OSError("C:/private/server/path/plan.md is unavailable")

    runtime.plan_documents.projector = FailingProjector()
    response = client.get(f"/api/plans/{document.id}/file", headers=_headers(app))

    assert response.status_code == 409
    assert response.json()["detail"] == "plan file unavailable"
    assert "private/server/path" not in response.text


def test_plan_restore_requires_both_version_and_hash_cas_values(tmp_path) -> None:
    runtime, app, client, thread, first = _seed(tmp_path)
    document = runtime.plan_documents.get_by_thread(thread.id)
    response = client.post(
        f"/api/plans/{document.id}/restore",
        headers=_headers(app),
        json={"version": first.version, "expected_version": first.version},
    )

    assert response.status_code == 422


def _count(runtime, table: str) -> int:
    with runtime.db.connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
