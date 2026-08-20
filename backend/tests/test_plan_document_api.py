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
    assert version.json()["markdown"] == "# Version 1\n"
    assert file_response.text == "# Version 1\n"
    assert file_response.headers["content-type"].startswith("text/markdown")
    assert all(count == 0 for count in (
        _count(runtime, "goals"),
        _count(runtime, "runs"),
        _count(runtime, "plan_versions"),
    ))


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
