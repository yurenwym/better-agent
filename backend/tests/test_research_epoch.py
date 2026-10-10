import pytest

from app.conversation import ConversationService
from app.db import Database
from app.research.models import ResearchEvent
from app.research.service import ResearchService
from app.research.worker import ManagedResearchWorker


@pytest.mark.asyncio
async def test_worker_commits_under_claimed_epoch(tmp_path):
    class Engine:
        async def run_research(self, request):
            yield ResearchEvent("section", "writing", {"ordinal": 1, "heading": "one", "markdown": "one"})
            yield ResearchEvent("report", "completed", {"title": "report", "markdown": "report", "source_count": 0, "evidence_count": 0})
    db = Database(tmp_path / "worker.db")
    try:
        conversation = ConversationService(db)
        service = ResearchService(db, conversation.events, Engine())
        job = service.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
        await ManagedResearchWorker(service).run_once(job.id)
        assert service.get(job.id).status == "COMPLETED"
        assert service.get(job.id).lease_epoch == 1
        assert service.completed_sections(job.id)[1]["markdown"] == "one"
    finally:
        db.close()


def test_same_worker_takeover_rejects_all_old_writes(tmp_path):
    db = Database(tmp_path / "epoch.db")
    try:
        conversation = ConversationService(db)
        service = ResearchService(db, conversation.events)
        job = service.create_manual(conversation.create_thread().id, "topic", "one", ("web",))
        first = service.claim(job.id, "worker", 30)
        with db.transaction() as connection:
            connection.execute("UPDATE research_jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (job.id,))
        second = service.claim_next("worker", 30)
        assert second.lease_epoch > first.lease_epoch
        assert not service.renew(job.id, "worker", 30, epoch=first.lease_epoch)
        for action in (
            lambda: service.set_phase(job.id, "worker", "writing", epoch=first.lease_epoch),
            lambda: service.apply_event(job.id, "worker", ResearchEvent("section", "writing", {"ordinal": 1, "heading": "late"}), epoch=first.lease_epoch),
            lambda: service.complete(job.id, "worker", "late", "late", 0, 0, epoch=first.lease_epoch),
            lambda: service.fail(job.id, "worker", "late", epoch=first.lease_epoch),
            lambda: service.finish_cancelled(job.id, "worker", epoch=first.lease_epoch),
        ):
            with pytest.raises(PermissionError):
                action()
        assert service.completed_sections(job.id) == {}
        service.complete(job.id, "worker", "new", "new", 0, 0, epoch=second.lease_epoch)
        with pytest.raises(PermissionError):
            service.complete(job.id, "worker", "new", "new", 0, 0, epoch=first.lease_epoch)
        assert service.get(job.id).report_markdown == "new"
    finally:
        db.close()
