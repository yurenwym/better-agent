from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.conversation import ConversationService
from app.db import Database
from app.main import create_app
from app.memory_archive import ConversationArchiver
from app.memory_v2 import MemoryStore


def test_archive_status_hides_other_owners_and_requires_csrf_for_retry(tmp_path):
    db = Database(tmp_path / "test.db")
    conversation = ConversationService(db)
    thread = conversation.create_thread(owner_id="alice")
    archiver = ConversationArchiver(db, MemoryStore(db, tmp_path / "memory"))
    app = create_app(runtime=SimpleNamespace(archiver=archiver))
    client = TestClient(app)
    route = f"/api/threads/{thread.id}/archive"
    headers = {"host":"127.0.0.1:8000", "x-owner-id":"alice"}
    assert client.get(route, headers=headers).json() == {"archived_through_seq":0,"jobs":[]}
    assert client.get(route, headers={**headers,"x-owner-id":"bob"}).status_code == 404
    assert client.post(route + "/missing/retry", headers=headers, json={"expected_updated_at":"now"}).status_code == 403
