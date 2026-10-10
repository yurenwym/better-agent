import httpx
import pytest

from app.model_gateway import ModelGateway, ModelProfile, ModelRequest
from app.send_authority import send_authority
from test_research_service import build


@pytest.mark.asyncio
async def test_same_worker_reclaim_before_send_blocks_old_attempt(tmp_path, monkeypatch):
    db, conversation, research = build(tmp_path)
    job = research.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
    first = research.claim(job.id, "worker", 60)
    monkeypatch.setenv("LEASE_TEST_KEY", "offline")
    sent = []
    gateway = ModelGateway(ModelProfile("https://offline.test", "fake", "LEASE_TEST_KEY", max_attempts=1),
        transport=httpx.MockTransport(lambda request: sent.append(request)))

    def reclaim(*_):
        with db.transaction() as connection:
            connection.execute("UPDATE research_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (job.id,))
        second = research.claim_next("worker", 60)
        assert second.lease_epoch > first.lease_epoch

    def require():
        with db.transaction() as connection:
            research._owned(job.id, "worker", connection, epoch=first.lease_epoch)

    try:
        with send_authority(require), pytest.raises(PermissionError):
            await gateway.complete(ModelRequest([{"role": "user", "content": "hello"}]), on_attempt_started=reclaim)
        assert sent == []
    finally:
        db.close()
