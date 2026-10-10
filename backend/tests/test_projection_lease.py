import pytest

from app.plan_execution import source_from_document
from app.runtime import PlanDraft
from app.task_runtime import LeaseLost
from test_materializer import MaterializerModel
from test_runtime import make_runtime


@pytest.mark.asyncio
async def test_same_materializer_takeover_rejects_late_draft(tmp_path):
    runtime = make_runtime(tmp_path, MaterializerModel())
    try:
        thread = runtime.conversation.create_thread("projection")
        runtime.plan_documents.save_model_revision(
            thread_id=thread.id, title="Plan", markdown_content="# Plan\n",
            source_turn_id=None, source_message_id=None, actor="model",
        )
        accepted = runtime.conversation.accept_turn(thread.id, "one", "execute", [])
        await runtime.turn_worker.run_once()
        turn = runtime.conversation.turn(accepted.turn_id)
        document = runtime.plan_documents.get_by_thread(thread.id)
        source = source_from_document(runtime.plan_documents.current_version(document.id))
        materializer = runtime.conversation.materializer
        first = materializer._claim_projection(turn.id, "first", turn.version, source, thread.id)
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE turns SET direction_projection_lease_until='2000-01-01T00:00:00+00:00' WHERE id=?", (turn.id,))
        with pytest.raises(LeaseLost):
            materializer._assert_projection(first)
        second = materializer._claim_projection(turn.id, "second", turn.version, source, thread.id)
        assert second.epoch == first.epoch + 1
        draft = PlanDraft([{"id": "s", "title": "work"}], "ready")
        with pytest.raises(LeaseLost):
            materializer._store_projection_draft(turn.id, "first", source, draft, token=first)
        materializer._fail_projection(turn.id, "first", "late error", thread.id, token=first)
        materializer._store_projection_draft(turn.id, "second", source, draft, token=second)
        with runtime.db.connection() as connection:
            row = connection.execute("SELECT direction_projection_status,direction_projection_error FROM turns WHERE id=?", (turn.id,)).fetchone()
            assert row["direction_projection_status"] == "READY"
            assert row["direction_projection_error"] is None
    finally:
        runtime.db.close()
