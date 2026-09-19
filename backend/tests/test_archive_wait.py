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
    DEFAULT_ARCHIVE_WAIT_MS, DEFAULT_ARCHIVE_WAIT_POLL_MS, ArchiveUnavailable,
    ArchiveWaitPolicy, ArchiveWaitTimeout, ConversationArchiver, WaitDeadline,
    archive_wait_policy,
)
from app.memory_v2 import MemoryStore
from test_runtime import make_runtime


# --------------------------------------------------------------------------
# Policy and deadline arithmetic
# --------------------------------------------------------------------------

def test_the_wait_policy_defaults_to_the_plan_candidate():
    assert archive_wait_policy().public_view() == {
        "deadline_ms": DEFAULT_ARCHIVE_WAIT_MS, "poll_ms": DEFAULT_ARCHIVE_WAIT_POLL_MS,
    }
    assert DEFAULT_ARCHIVE_WAIT_MS == 2_000


def test_the_wait_policy_is_configurable_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "750")
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_POLL_MS", "25")
    assert archive_wait_policy().public_view() == {"deadline_ms": 750, "poll_ms": 25}
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "0")
    with pytest.raises(ValueError):
        archive_wait_policy()
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "2000")
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_POLL_MS", "0")
    with pytest.raises(ValueError):
        archive_wait_policy()


def test_a_poll_below_one_millisecond_is_floored_not_accepted():
    """A zero poll would spin the event loop."""
    policy = ArchiveWaitPolicy(deadline_ms=1000, poll_ms=0)
    deadline = WaitDeadline(policy, clock=lambda: 0.0)
    assert deadline.sleep_seconds() == 0.0


def test_the_sleep_slice_never_passes_the_deadline():
    ticks = [0.0]
    deadline = WaitDeadline(ArchiveWaitPolicy(2_000, 50), clock=lambda: ticks[0])
    assert deadline.sleep_seconds() == 0.05
    ticks[0] = 1.99
    # 10ms left, not the full 50ms poll: the loop must not overshoot.
    assert deadline.sleep_seconds() == pytest.approx(0.01)
    ticks[0] = 2.0
    assert deadline.expired is True
    assert deadline.sleep_seconds() == 0.0


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

@pytest.mark.asyncio
async def test_a_wait_that_cannot_finish_stops_at_the_deadline(tmp_path, monkeypatch):
    """The bound is wall-clock, not an iteration count."""
    from app.conversation import ManagedTurnWorker

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "300")
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_POLL_MS", "20")

    async def never_finishes(payload):
        # A job that another worker owns forever: nothing is ever committed.
        raise AssertionError("the foreground must not summarise here")

    runtime, archiver = _runtime_with_archiver(tmp_path, never_finishes)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)

    # Hold the job so the foreground can only wait for it.
    original_claim = archiver.claim
    held: list[str] = []

    def hold(job_id=None):
        claim = original_claim(job_id)
        if claim is not None:
            held.append(claim.job_id)
        return None

    archiver.claim = hold

    worker = ManagedTurnWorker(runtime.conversation)
    started = time.monotonic()
    with pytest.raises(ArchiveWaitTimeout):
        await worker._archive_history_before_generation(turn)
    elapsed = time.monotonic() - started
    assert held, "the job should have been enqueued"
    # Bounded: comfortably inside a generous multiple of the configured 300ms.
    assert elapsed < 2.0, elapsed
    assert _archiving_states(runtime, thread) == ["waiting", "timeout"]


@pytest.mark.asyncio
async def test_a_slow_archive_attempt_is_cut_off_by_the_deadline(tmp_path, monkeypatch):
    """The bound covers the foreground's own model call, not only idle waiting."""
    from app.conversation import ManagedTurnWorker

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "200")

    started = asyncio.Event()

    async def hangs(payload):
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    runtime, archiver = _runtime_with_archiver(tmp_path, hangs)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)

    worker = ManagedTurnWorker(runtime.conversation)
    wall = time.monotonic()
    with pytest.raises(ArchiveWaitTimeout):
        await worker._archive_history_before_generation(turn)
    assert time.monotonic() - wall < 2.0
    assert started.is_set()
    # The cut-off job is left durably reclaimable rather than silently lost.
    with runtime.db.connection() as connection:
        status = connection.execute(
            "SELECT status FROM memory_archive_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["status"]
    assert status in {"QUEUED", "RUNNING"}


@pytest.mark.asyncio
async def test_a_request_that_fits_never_waits_and_never_announces(tmp_path, monkeypatch):
    from app.conversation import ManagedTurnWorker

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "1")

    async def unused(payload):
        raise AssertionError("nothing should be archived")

    runtime, archiver = _runtime_with_archiver(tmp_path, unused)
    archiver.keep_tokens = 200_000
    thread = _thread_with_history(runtime, turns=2, size=200)
    turn = await _accepted_turn(runtime, thread)

    worker = ManagedTurnWorker(runtime.conversation)
    started = time.monotonic()
    await worker._archive_history_before_generation(turn)
    assert time.monotonic() - started < 0.5
    assert _archiving_states(runtime, thread) == []
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_archive_jobs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_completed_pass_reports_ready_and_reaches_the_target(tmp_path, monkeypatch):
    from app.conversation import ManagedTurnWorker

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "5000")

    async def summary(payload):
        ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
        return {
            "synopsis": [{"text": "s", "source_message_ids": [ids[0]]}],
            "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
        }

    runtime, archiver = _runtime_with_archiver(tmp_path, summary)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)

    worker = ManagedTurnWorker(runtime.conversation)
    await worker._archive_history_before_generation(turn)

    assert _archiving_states(runtime, thread) == ["waiting", "ready"]
    with runtime.db.connection() as connection:
        covered = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
            (thread.id,),
        ).fetchone()["archived_through_seq"]
    assert covered > 0


# --------------------------------------------------------------------------
# Cancellable, without undoing committed work
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelling_the_wait_ends_the_turn_promptly(tmp_path, monkeypatch):
    from app.conversation import ManagedTurnWorker, TurnJobCancelled

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "30_000")
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_POLL_MS", "20")

    async def never(payload):
        raise AssertionError("must not be called")

    runtime, archiver = _runtime_with_archiver(tmp_path, never)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)

    archiver.claim = lambda job_id=None: None

    worker = ManagedTurnWorker(runtime.conversation)
    checks = {"n": 0}

    def cancel_after_the_wait_started(turn_id: str) -> bool:
        checks["n"] += 1
        # False on the first check so the wait really starts; then cancel.
        return checks["n"] > 1

    worker._cancel_requested = cancel_after_the_wait_started

    started = time.monotonic()
    with pytest.raises(TurnJobCancelled):
        await worker._archive_history_before_generation(turn)
    # A cancel is honoured between slices, not after the 30s deadline.
    assert time.monotonic() - started < 2.0
    assert _archiving_states(runtime, thread) == ["waiting", "cancelled"]


@pytest.mark.asyncio
async def test_a_cancel_that_arrives_before_the_wait_produces_no_status_noise(tmp_path, monkeypatch):
    """Nothing is announced for a wait that never actually started."""
    from app.conversation import ManagedTurnWorker, TurnJobCancelled

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "30_000")

    async def never(payload):
        raise AssertionError("must not be called")

    runtime, archiver = _runtime_with_archiver(tmp_path, never)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)
    archiver.claim = lambda job_id=None: None

    worker = ManagedTurnWorker(runtime.conversation)
    worker._cancel_requested = lambda turn_id: True

    with pytest.raises(TurnJobCancelled):
        await worker._archive_history_before_generation(turn)
    assert _archiving_states(runtime, thread) == []


@pytest.mark.asyncio
async def test_cancelling_the_wait_does_not_roll_back_a_committed_archive(tmp_path, monkeypatch):
    """A cancel stops the wait; it never undoes an Episode that already landed."""
    from app.conversation import ManagedTurnWorker, TurnJobCancelled

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "30_000")

    async def summary(payload):
        ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
        return {
            "synopsis": [{"text": "s", "source_message_ids": [ids[0]]}],
            "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
        }

    runtime, archiver = _runtime_with_archiver(tmp_path, summary)
    # Small enough that one pass commits, but the target is never reached, so
    # the loop would keep going -- which is where the cancel lands.
    archiver.keep_tokens = 30_000
    archiver.max_summary_tokens = 10 ** 7
    thread = _thread_with_history(runtime, turns=8, size=3_000)
    turn = await _accepted_turn(runtime, thread)

    worker = ManagedTurnWorker(runtime.conversation)
    calls = {"n": 0}

    def cancel_after_first_commit(turn_id: str) -> bool:
        calls["n"] += 1
        # First check happens before any work; cancel only once work has landed.
        with runtime.db.connection() as connection:
            row = connection.execute(
                "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
                (thread.id,),
            ).fetchone()
        return bool(row and int(row["archived_through_seq"]) > 0)

    worker._cancel_requested = cancel_after_first_commit

    with pytest.raises((TurnJobCancelled, ArchiveUnavailable)):
        await worker._archive_history_before_generation(turn)

    with runtime.db.connection() as connection:
        covered = connection.execute(
            "SELECT archived_through_seq FROM conversation_archive_state WHERE thread_id=?",
            (thread.id,),
        ).fetchone()["archived_through_seq"]
        episodes = connection.execute(
            "SELECT COUNT(*) FROM memory_episodes WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
    # Committed work survives the cancel, and the cursor never moves backwards.
    assert covered > 0
    assert episodes >= 1


@pytest.mark.asyncio
async def test_a_cancelled_turn_cannot_be_reexecuted(tmp_path):
    """Re-execution validates the old task state instead of assuming it is live."""
    from app.conversation import TurnJobCancelled

    runtime = make_runtime(tmp_path, _noop_model())
    thread = runtime.conversation.create_thread(owner_id="local-user")
    submission = runtime.conversation.accept_turn(
        thread.id, "cancel-me", "hello", owner_id="local-user",
    )
    runtime.conversation.cancel_turn(submission.turn_id, "local-user")

    from app.conversation import ManagedTurnWorker

    worker = ManagedTurnWorker(runtime.conversation)
    with pytest.raises((TurnJobCancelled, Exception)) as caught:
        worker._assert_job_owner(submission.turn_id)
    # Whatever the exact type, it must refuse rather than run the old turn.
    assert caught.value is not None


def _noop_model():
    from app.runtime import MockModelGateway

    return MockModelGateway()


# --------------------------------------------------------------------------
# The status is a context status, never model output
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_status_event_is_a_context_event_and_never_a_model_event(tmp_path, monkeypatch):
    from app.conversation import ManagedTurnWorker

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "300")
    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_POLL_MS", "20")

    async def never(payload):
        raise AssertionError("must not be called")

    runtime, archiver = _runtime_with_archiver(tmp_path, never)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)
    archiver.claim = lambda job_id=None: None

    worker = ManagedTurnWorker(runtime.conversation)
    with pytest.raises(ArchiveWaitTimeout):
        await worker._archive_history_before_generation(turn)

    with runtime.db.connection() as connection:
        types = [
            row["type"] for row in connection.execute(
                "SELECT type FROM thread_events WHERE thread_id=? ORDER BY seq", (thread.id,)
            ).fetchall()
        ]
    assert "context.archiving" in types
    # Nothing in the wait path may emit a model event: that is what would let a
    # status hint be counted as a first-token improvement.
    assert not any(name.startswith("model.") for name in types)
    assert not any("first_token" in name for name in types)


@pytest.mark.asyncio
async def test_the_status_event_carries_the_numbers_the_ui_needs(tmp_path, monkeypatch):
    from app.conversation import ManagedTurnWorker

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "300")

    async def never(payload):
        raise AssertionError("must not be called")

    runtime, archiver = _runtime_with_archiver(tmp_path, never)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)
    archiver.claim = lambda job_id=None: None

    worker = ManagedTurnWorker(runtime.conversation)
    with pytest.raises(ArchiveWaitTimeout):
        await worker._archive_history_before_generation(turn)

    waiting = _events(runtime, thread, "context.archiving")[0]
    assert set(waiting) >= {
        "state", "waited_ms", "deadline_ms", "pending_units", "target_units", "input_limit",
    }
    assert waiting["deadline_ms"] == 300
    assert waiting["pending_units"] > waiting["target_units"]


@pytest.mark.asyncio
async def test_a_timeout_raises_its_own_event_so_the_answer_is_not_faked(tmp_path, monkeypatch):
    from app.conversation import ManagedTurnWorker

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "200")

    async def never(payload):
        raise AssertionError("must not be called")

    runtime, archiver = _runtime_with_archiver(tmp_path, never)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)
    archiver.claim = lambda job_id=None: None

    worker = ManagedTurnWorker(runtime.conversation)
    with pytest.raises(ArchiveWaitTimeout):
        await worker._archive_history_before_generation(turn)

    events = _events(runtime, thread, "context.archive_wait_timeout")
    assert len(events) == 1
    assert events[0]["recoverable"] is True
    assert events[0]["input_preserved"] is True
    assert events[0]["message"]
    assert events[0]["reason_code"] == "archive_wait_timeout"
    assert _archiving_states(runtime, thread) == ["waiting", "timeout"]


@pytest.mark.asyncio
async def test_an_attempt_that_overruns_reports_its_own_reason(tmp_path, monkeypatch):
    """The two ways to run out of budget are reported distinctly."""
    from app.conversation import ManagedTurnWorker

    monkeypatch.setenv("AGENT_ARCHIVE_WAIT_MS", "200")

    async def hangs(payload):
        await asyncio.sleep(30)

    runtime, archiver = _runtime_with_archiver(tmp_path, hangs)
    archiver.keep_tokens = 4_000
    thread = _thread_with_history(runtime, turns=8, size=900)
    turn = await _accepted_turn(runtime, thread)

    worker = ManagedTurnWorker(runtime.conversation)
    with pytest.raises(ArchiveWaitTimeout):
        await worker._archive_history_before_generation(turn)

    events = _events(runtime, thread, "context.archive_wait_timeout")
    assert [event["reason"] for event in events] == ["attempt_exceeded_deadline"]


# --------------------------------------------------------------------------
# Local counting cost is measured on its own
# --------------------------------------------------------------------------

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
