"""R2-04: the bounded, cancellable foreground archival wait.

The properties under test are the ones the plan makes acceptance conditions:
the wait has a deadline, it can be cancelled, cancelling does not undo work that
already committed, a request that fits never waits at all, and the status the
user sees is a context status rather than model output.
"""

import asyncio
import json
import time

import pytest

from app.db import Database
from app.memory_archive import (
    ArchiveUnavailable, ArchiveWaitTimeout, ConversationArchiver, WaitDeadline,
)
from app.memory_v2 import MemoryStore
from test_runtime import make_runtime


# --------------------------------------------------------------------------
# Policy and deadline arithmetic
# --------------------------------------------------------------------------

def test_local_measurement_deadline_is_bounded():
    ticks = [0.0]
    deadline = WaitDeadline(2000, clock=lambda: ticks[0])
    assert deadline.remaining_ms == 2000
    ticks[0] = 1.99
    assert deadline.remaining_ms == 10
    ticks[0] = 2.0
    assert deadline.expired and deadline.remaining_ms == 0


def test_the_timeout_is_a_distinct_recoverable_code():
    assert ArchiveWaitTimeout.code == "archive_wait_timeout"
    assert issubclass(ArchiveWaitTimeout, ArchiveUnavailable)
    assert ArchiveWaitTimeout.code != ArchiveUnavailable.code
    assert ArchiveWaitTimeout.public_message


# --------------------------------------------------------------------------
# A real thread that must be compacted
# --------------------------------------------------------------------------

def _runtime_with_archiver(tmp_path, summarizer):
    from app.model_gateway import ModelProfile
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    archiver = ConversationArchiver(runtime.db, MemoryStore(runtime.db, tmp_path / "memory"), summarizer)
    runtime.archiver = archiver
    runtime.model_control = None
    return runtime, archiver


def _thread_with_history(runtime, *, turns: int, size: int, owner_id: str = "local-user"):
    thread = runtime.conversation.create_thread(owner_id=owner_id)
    now = "2026-01-01T00:00:00+00:00"
    with runtime.db.transaction() as connection:
        for index in range(turns):
            turn_id = f"hist-{index}"
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED',?,?)", (turn_id, thread.id, turn_id, now, now),
            )
            for role, offset in (("user", 1), ("assistant", 2)):
                connection.execute(
                    "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                    "content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                    (f"m-{index}-{role}", thread.id, turn_id, role, "x" * size, size,
                     index * 2 + offset, now, now),
                )
    return thread


async def _accepted_turn(runtime, thread, *, owner_id="local-user"):
    submission = runtime.conversation.accept_turn(
        thread.id, "live-turn", "现在的问题", owner_id=owner_id,
    )
    return runtime.conversation.turn(submission.turn_id, owner_id)


def _events(runtime, thread, event_type):
    with runtime.db.connection() as connection:
        rows = connection.execute(
            "SELECT data_json FROM thread_events WHERE thread_id=? AND type=? ORDER BY seq",
            (thread.id, event_type),
        ).fetchall()
    return [json.loads(row["data_json"]) for row in rows]


def _archiving_states(runtime, thread):
    return [data["state"] for data in _events(runtime, thread, "context.archiving")]


# --------------------------------------------------------------------------
# Bounded
# --------------------------------------------------------------------------

async def _summary(payload):
    ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
    return {"synopsis": [{"text": "历史要点", "source_message_ids": [ids[0]]}],
        "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal"}


async def _waiting_case(tmp_path, summarizer=_summary):
    from app.conversation import ManagedTurnWorker
    from app.memory_archive import ArchivePending
    runtime, archiver = _runtime_with_archiver(tmp_path, summarizer)
    archiver.keep_tokens = 4000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)
    worker = ManagedTurnWorker(runtime.conversation)
    assert worker.claim_next() == turn.id
    with pytest.raises(ArchivePending):
        await worker._archive_history_before_generation(turn)
    return runtime, archiver, thread, turn, worker


@pytest.mark.asyncio
async def test_pending_archive_releases_slot_and_does_not_run_inline(tmp_path):
    called = []
    async def summary(payload):
        called.append(True)
        return await _summary(payload)
    runtime, archiver, thread, turn, worker = await _waiting_case(tmp_path, summary)
    assert not called
    with runtime.db.connection() as connection:
        job = connection.execute("SELECT * FROM turn_jobs WHERE turn_id=?", (turn.id,)).fetchone()
    assert job["status"] == "QUEUED" and job["lease_owner"] is None and job["archive_job_id"]
    assert worker.claim_next() is None
    from app.memory_archive import foreground_turn_pending
    assert not foreground_turn_pending(runtime.db)
    assert not _events(runtime, thread, "model.started")
    assert _archiving_states(runtime, thread)[-1] == "waiting"
    runtime.db.close()


@pytest.mark.asyncio
async def test_later_turn_does_not_overtake_archive_wait_or_starve_archiver(tmp_path):
    runtime, archiver, thread, turn, worker = await _waiting_case(tmp_path)
    runtime.conversation.accept_turn(thread.id, "later-turn", "稍后查询", owner_id="local-user")
    assert worker.claim_next() is None
    from app.memory_archive import foreground_turn_pending
    assert not foreground_turn_pending(runtime.db)
    runtime.db.close()


@pytest.mark.asyncio
async def test_lost_archive_lease_fails_instead_of_spinning(tmp_path):
    runtime, archiver, thread, turn, worker = await _waiting_case(tmp_path)
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE memory_archive_jobs SET status='LEASE_LOST'")
    assert worker.claim_next() == turn.id
    with pytest.raises(ArchiveUnavailable):
        await worker._archive_history_before_generation(turn)
    runtime.db.close()


@pytest.mark.asyncio
async def test_slow_archive_completes_and_new_worker_resumes_original_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "1")
    async def slow(payload):
        await asyncio.sleep(.05)
        return await _summary(payload)
    runtime, archiver, thread, turn, worker = await _waiting_case(tmp_path, slow)
    from app.memory_archive import ManagedArchiveWorker
    archive_worker = ManagedArchiveWorker(archiver)
    assert await archive_worker.run_once()
    from app.conversation import ManagedTurnWorker
    recovered = ManagedTurnWorker(runtime.conversation)
    assert recovered.claim_next() == turn.id
    await recovered._archive_history_before_generation(turn)
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?", (thread.id,)).fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM thread_messages WHERE turn_id=? AND role='user'", (turn.id,)).fetchone()[0] == 1
    runtime.db.close()


@pytest.mark.asyncio
async def test_cancel_wait_does_not_reexecute_turn_or_rollback_archive(tmp_path):
    runtime, archiver, thread, turn, worker = await _waiting_case(tmp_path)
    runtime.conversation.cancel_turn(turn.id, "local-user")
    assert worker.claim_next() is None
    claim = archiver.claim()
    assert claim
    await archiver.process(claim)
    assert runtime.conversation.turn(turn.id).status == "CANCELLED"
    assert worker.claim_next() is None
    runtime.db.close()


@pytest.mark.asyncio
async def test_total_wait_timeout_becomes_eligible_without_archiver_finishing(tmp_path):
    runtime, archiver, thread, turn, worker = await _waiting_case(tmp_path)
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE turn_jobs SET archive_wait_until=? WHERE turn_id=?", ("2000-01-01T00:00:00+00:00", turn.id))
    assert worker.claim_next() == turn.id
    with pytest.raises(ArchiveWaitTimeout):
        await worker._archive_history_before_generation(turn)
    runtime.db.close()


@pytest.mark.asyncio
async def test_permanent_archive_failure_does_not_loop(tmp_path):
    runtime, archiver, thread, turn, worker = await _waiting_case(tmp_path)
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE memory_archive_jobs SET status='DEAD_LETTER'")
        # A proactive archive may fail before this turn ever waits for it.
        connection.execute("UPDATE turn_jobs SET archive_job_id=NULL WHERE turn_id=?", (turn.id,))
        connection.execute("UPDATE turns SET reason_code=NULL WHERE id=?", (turn.id,))
    assert worker.claim_next() == turn.id
    with pytest.raises(ArchiveUnavailable):
        await worker._archive_history_before_generation(turn)
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT archive_job_id FROM turn_jobs WHERE turn_id=?", (turn.id,)).fetchone()[0]
    assert runtime.conversation.turn(turn.id).reason_code == "archive_unavailable"
    runtime.db.close()


@pytest.mark.asyncio
async def test_request_that_fits_does_not_enqueue(tmp_path):
    from app.conversation import ManagedTurnWorker
    runtime, archiver = _runtime_with_archiver(tmp_path, _summary)
    thread = _thread_with_history(runtime, turns=1, size=20)
    turn = await _accepted_turn(runtime, thread)
    await ManagedTurnWorker(runtime.conversation)._archive_history_before_generation(turn)
    assert not archiver.status(thread.id, "local-user")["jobs"]
    runtime.db.close()


def test_the_local_counting_cost_is_reported_separately_from_the_whole_context(tmp_path):
    from app.conversation import ConversationService

    db = Database(tmp_path / "counted.db")
    conversation = ConversationService(db)
    thread = conversation.create_thread(owner_id="local-user")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
            "VALUES ('t1',?,'t1','COMPLETED','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (thread.id,),
        )
        connection.execute(
            "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
            "content_length,message_seq,created_at,completed_at) "
            "VALUES ('m1',?,'t1','user','hello','ready',1,5,1,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            (thread.id,),
        )

    from app.token_budget import hot_window
    from app.model_gateway import ModelProfile

    profile = ModelProfile(
        base_url="https://api.deepseek.com", model="m", api_key_env="K",
        context_window=32768, max_output_tokens=8192,
    )
    conversation.events  # ensure the store is reachable
    # Drive the selector directly: it is what R2 added to the answer path.
    class _Worker:
        def __init__(self, conversation):
            self.conversation = conversation
            self.db = conversation.db

    from app.conversation import ManagedTurnWorker

    worker = ManagedTurnWorker(conversation)
    history = worker._history(thread.id, "live", hot_window(profile))
    assert history
    events = _events_for(db, thread.id, "context.counted")
    assert len(events) == 1
    assert events[0]["counting_ms"] >= 0
    assert events[0]["kept_turns"] == 1
    assert events[0]["input_limit"] == hot_window(profile).input_limit


def _events_for(db, thread_id, event_type):
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT data_json FROM thread_events WHERE thread_id=? AND type=? ORDER BY seq",
            (thread_id, event_type),
        ).fetchall()
    return [json.loads(row["data_json"]) for row in rows]
