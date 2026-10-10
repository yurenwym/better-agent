from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.conversation import ConversationService
from app.db import Database
from app.main import create_app
from app.memory_archive import ConversationArchiver
from app.memory_v2 import MemoryStore
import pytest


def test_archive_status_hides_other_owners_and_requires_csrf_for_retry(tmp_path):
    db = Database(tmp_path / "test.db")
    conversation = ConversationService(db)
    thread = conversation.create_thread(owner_id="alice")
    archiver = ConversationArchiver(db, MemoryStore(db, tmp_path / "memory"))
    app = create_app(runtime=SimpleNamespace(archiver=archiver))
    client = TestClient(app)
    route = f"/api/threads/{thread.id}/archive"
    headers = {"host":"127.0.0.1:8000", "x-owner-id":"alice"}
    assert client.get(route, headers=headers).json() == {"archived_through_seq":0,"jobs":[],"waiting_turn":None}
    assert client.get(route, headers={**headers,"x-owner-id":"bob"}).status_code == 404
    assert client.post(route + "/missing/retry", headers=headers, json={"expected_updated_at":"now"}).status_code == 403


@pytest.mark.asyncio
async def test_retry_resumes_saved_turn_once_and_keeps_budget_and_input(tmp_path):
    from test_archive_wait import _waiting_case
    runtime, archiver, thread, turn, worker = await _waiting_case(tmp_path)
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE turns SET status='FAILED' WHERE id=?", (turn.id,))
        connection.execute("UPDATE turn_jobs SET status='FAILED' WHERE turn_id=?", (turn.id,))
        connection.execute("UPDATE memory_archive_jobs SET status='DEAD_LETTER'")
    original = runtime.conversation.turn(turn.id)
    app = create_app(runtime=runtime)
    client = TestClient(app)
    headers = {"host": "127.0.0.1:8000", "x-owner-id": "local-user", "x-csrf-token": app.state.csrf_token}
    route = f"/api/turns/{turn.id}/retry-archive"
    assert client.post(route, headers={**headers, "x-owner-id": "other"}, json={"expected_version": original.version}).status_code == 404
    assert client.post(route, headers=headers, json={"expected_version": original.version + 1}).status_code == 409
    assert client.post(route, headers=headers, json={"expected_version": original.version}).status_code == 200
    assert client.post(route, headers=headers, json={"expected_version": original.version}).status_code == 200
    resumed = runtime.conversation.turn(turn.id)
    assert resumed.status == "ROUTING" and resumed.root_budget_id == original.root_budget_id
    assert resumed.version == original.version + 1
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM thread_messages WHERE turn_id=? AND role='user'", (turn.id,)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM memory_archive_jobs").fetchone()[0] == 1
    runtime.db.close()
