from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.behavior import BehaviorBundleService
from app.db import Database
from app.domain import Checkpoint, CheckpointStore
from app.evolution import EvolutionService
from app.growth import GrowthProfileService


def _database(database_url, tmp_path) -> Database:
    return Database(database_url, workspace=tmp_path / "artifacts")


def test_review_lookup_does_not_open_sqlite(migrated_postgres_url, tmp_path, monkeypatch):
    from app.goal_reviews import GoalReviewService
    database = _database(migrated_postgres_url, tmp_path)
    try:
        def forbidden():
            raise AssertionError("SQLite must not be opened")
        monkeypatch.setattr(database, "_connect", forbidden)
        service = GoalReviewService(SimpleNamespace(db=database), None, None)
        assert service.for_program_date("missing", "2026-09-08") is None
    finally:
        database.close()


def test_observer_offset_upsert_on_postgres(migrated_postgres_url, tmp_path):
    from app.experience_observer import ExperienceObserver
    from app.events import EventStore, ThreadEventStore
    database = _database(migrated_postgres_url, tmp_path)
    try:
        bundles = BehaviorBundleService(database)
        bundle = bundles.ensure({"prompt":"test"})
        bundles.activate("stable", bundle.id, "observer-test")
        evolution = EvolutionService(database, bundles)
        observer = ExperienceObserver(database, EventStore(database), evolution, thread_events=ThreadEventStore(database))
        assert observer.observe()["created"] == 0
        assert observer.observe()["created"] == 0
        with database.connection() as connection:
            offsets = connection.execute("SELECT stream_kind,last_row_id FROM evolution_observer_offsets WHERE owner_id='local-user'").fetchall()
            assert len(offsets) == 4
            # No source rows were read. Sequence allocation is not a committed
            # source event and must not advance any stream's watermark.
            assert all(row["last_row_id"] == 0 for row in offsets)
    finally:
        database.close()


@pytest.mark.asyncio
async def test_conversation_archive_commits_on_postgres(migrated_postgres_url, tmp_path):
    from app.conversation import ConversationService, ManagedTurnWorker
    from app.memory_archive import ConversationArchiver
    from app.memory_v2 import MemoryStore

    database = _database(migrated_postgres_url, tmp_path)
    try:
        class Model:
            async def route_and_respond(self, *, on_text_delta, **kwargs):
                on_text_delta('{"v":1,"policy":"answer","content_shape":"text","reason_code":"test"}\nAnswer')
        conversation = ConversationService(database, route_model=Model())
        thread = conversation.create_thread("Archive compatibility")
        conversation.accept_turn(thread.id, "archive-test", "Question", [])
        await ManagedTurnWorker(conversation).run_once()

        async def summary(payload):
            source_id = payload["turns"][0]["events"][0]["message_id"]
            return {"synopsis":[{"text":"The user asked a question.","source_message_ids":[source_id]}],
                    "topics":[],"decisions":[],"outcomes":[],"open_loops":[],"sensitivity":"normal"}

        archiver = ConversationArchiver(database, MemoryStore(database, tmp_path / "memory"), summary, keep_messages=0)
        episode = await archiver.archive_thread(thread.id)
        assert episode is not None
        with database.connection() as connection:
            state = connection.execute("SELECT archived_through_seq,state FROM conversation_archive_state WHERE thread_id=?", (thread.id,)).fetchone()
            assert state["archived_through_seq"] == episode.end_message_seq
            assert state["state"] == "IDLE"
    finally:
        database.close()


def test_checkpoint_latest_and_completed_tool_are_postgres_compatible(
    migrated_postgres_url, tmp_path
) -> None:
    database = _database(migrated_postgres_url, tmp_path)
    try:
        store = CheckpointStore(database)
        first = store.save(Checkpoint(
            run_id="compat-run", state="PLANNING", plan_version_id=None, step_id=None,
        ))
        second = store.save(Checkpoint(
            run_id="compat-run", state="EXECUTING", plan_version_id=None, step_id=None,
        ))

        assert store.latest("compat-run").id == second.id

        store.record_completed_tool("compat-run", "compat-call", {"value": 1})
        store.record_completed_tool("compat-run", "compat-call", {"value": 2})
        assert store.completed_tool_result("compat-run", "compat-call") == {"value": 2}
        assert first.id != second.id
    finally:
        database.close()


def test_growth_empty_profile_uses_portable_conditional_counts(
    migrated_postgres_url, tmp_path
) -> None:
    database = _database(migrated_postgres_url, tmp_path)
    try:
        service = GrowthProfileService(
            SimpleNamespace(db=database, goal_programs=SimpleNamespace())
        )
        result = service.profile(owner_id="compat-empty-owner")
        assert result["metrics"]["completed_actions"] == 0
        assert result["metrics"]["skipped_actions"] == 0
        assert result["metrics"]["deferred_actions"] == 0
    finally:
        database.close()


def test_evolution_bundle_listing_aggregates_channels_on_postgres(
    migrated_postgres_url, tmp_path
) -> None:
    database = _database(migrated_postgres_url, tmp_path)
    try:
        bundles = BehaviorBundleService(database)
        first = bundles.ensure({"version": 1})
        second = bundles.ensure({"version": 2})
        bundles.activate("stable", first.id, "compat-stable")
        bundles.activate("canary", first.id, "compat-canary")
        bundles.activate("candidate", second.id, "compat-candidate")

        listed = EvolutionService(database, bundles).list_bundles()
        by_id = {item["id"]: item for item in listed}
        assert by_id[first.id]["channels"] == ["canary", "stable"]
        assert by_id[second.id]["channels"] == ["candidate"]
    finally:
        database.close()
