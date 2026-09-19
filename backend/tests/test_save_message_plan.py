from fastapi.testclient import TestClient
from test_runtime import make_runtime
from test_api import _headers


def setup(tmp_path):
    from app.main import create_app
    from app.runtime import MockModelGateway
    runtime = make_runtime(tmp_path, MockModelGateway())
    thread = runtime.conversation.create_thread("Plan")
    with runtime.db.transaction() as connection:
        connection.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,version,created_at,updated_at) VALUES ('save-turn',?,'save-client','COMPLETED',0,'now','now')", (thread.id,))
        connection.execute("INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,created_at) VALUES ('save-message',?,'save-turn','assistant','# Plan\nPractice for 20 minutes.','ready','now')", (thread.id,))
    app = create_app(runtime=runtime)
    return runtime, TestClient(app), {**_headers(app), "Idempotency-Key": "save"}, f"/api/threads/{thread.id}/messages/save-message/save-plan"


def test_confirmed_message_save_is_durable_and_deduplicated(tmp_path):
    runtime, client, headers, url = setup(tmp_path)
    assert client.post(url, json={"title": "Plan", "confirmed": False}, headers=headers).status_code == 422
    first = client.post(url, json={"title": "Plan", "confirmed": True}, headers=headers)
    assert first.status_code == 200
    replay = client.post(url, json={"title": "Plan", "confirmed": True}, headers={**headers, "Idempotency-Key": "another-key"})
    assert replay.json() == first.json()
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM plan_document_versions").fetchone()[0] == 1
    revision = runtime.plan_documents.get_version(first.json()["plan_document_version_id"])
    assert revision.markdown_content == "# Plan\nPractice for 20 minutes."


def test_cannot_save_foreign_or_unfinished_message(tmp_path):
    runtime, client, headers, url = setup(tmp_path)
    payload = {"title": "Plan", "confirmed": True}
    assert client.post(url, json=payload, headers={**headers, "x-owner-id": "other"}).status_code == 404
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE thread_messages SET status='streaming' WHERE id='save-message'")
    assert client.post(url, json=payload, headers=headers).status_code == 422
