"""R2-03: the static early-archival line, its target, and the background limits.

The policy is arithmetic over the routed profile's own budget, so most of these
tests are exact numbers rather than ranges: a line that cannot be predicted from
``H`` and two ratios is not a *static* line.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app.db import Database
from app.memory_archive import (
    ConversationArchiver, ManagedArchiveWorker, foreground_turn_pending,
)
from app.memory_v2 import MemoryStore
from app.model_gateway import ModelProfile
from app.token_budget import (
    ARCHIVE_POLICY_VERSION, DEFAULT_ARCHIVE_RESERVE_RATIO, DEFAULT_ARCHIVE_TRIGGER_RATIO,
    StaticArchivePolicy, hot_window, static_archive_policy,
)


# The profile the R1 live acceptance used: 32768 window / 8192 output, which
# yields H = 23348.
def _profile(**overrides) -> ModelProfile:
    base = dict(
        base_url="https://api.deepseek.com", model="deepseek-flash", api_key_env="K",
        context_window=32768, max_output_tokens=8192,
    )
    base.update(overrides)
    return ModelProfile(**base)


H = 23348


# --------------------------------------------------------------------------
# Policy arithmetic
# --------------------------------------------------------------------------

def test_static_line_is_derived_from_h_and_two_ratios():
    policy = static_archive_policy(_profile())
    assert policy.input_limit == H
    assert (policy.trigger, policy.headroom, policy.reserve, policy.target) == (7004, 16344, 4669, 11675)
    assert policy.version == ARCHIVE_POLICY_VERSION


def test_trigger_and_headroom_partition_h():
    policy = static_archive_policy(_profile())
    assert policy.trigger + policy.headroom == policy.input_limit


def test_target_is_headroom_minus_reserve():
    policy = static_archive_policy(_profile())
    assert policy.target == policy.headroom - policy.reserve
    # And therefore T = H(1 - g - d) exactly.
    assert policy.target == H - policy.trigger - policy.reserve


def test_the_line_scales_with_the_model_not_with_a_byte_constant():
    small = static_archive_policy(_profile(context_window=8192, max_output_tokens=2048))
    large = static_archive_policy(_profile(context_window=200000, max_output_tokens=8192))
    assert small.trigger < large.trigger
    assert small.target < large.target
    # The ratios, not the absolute numbers, are what is fixed.
    assert small.trigger_ratio == large.trigger_ratio == DEFAULT_ARCHIVE_TRIGGER_RATIO
    assert small.reserve_ratio == large.reserve_ratio == DEFAULT_ARCHIVE_RESERVE_RATIO


def test_profile_declared_ratios_win_over_the_defaults():
    policy = static_archive_policy(_profile(archive_trigger_ratio=0.1, archive_reserve_ratio=0.4))
    assert policy.trigger_ratio == 0.1 and policy.reserve_ratio == 0.4
    assert policy.trigger == 2334
    assert policy.target == H - 2334 - 9339


def test_min_turns_travels_with_the_policy():
    assert static_archive_policy(_profile()).min_turns == 5
    assert static_archive_policy(_profile(history_min_turns=3)).min_turns == 3


def test_invalid_knobs_fall_back_instead_of_changing_the_line():
    policy = static_archive_policy(_profile(archive_trigger_ratio=0.0, archive_reserve_ratio=2.0))
    assert policy.trigger_ratio == DEFAULT_ARCHIVE_TRIGGER_RATIO
    assert policy.reserve_ratio == DEFAULT_ARCHIVE_RESERVE_RATIO


@pytest.mark.parametrize("trigger,reserve", [(0.0, 0.2), (1.0, 0.2), (-0.1, 0.2), (0.6, 0.5), (0.3, 1.0)])
def test_a_pair_that_leaves_no_positive_target_is_rejected(trigger, reserve):
    with pytest.raises(ValueError):
        static_archive_policy(_profile(), trigger_ratio=trigger, reserve_ratio=reserve)


def test_zero_reserve_is_allowed():
    policy = static_archive_policy(_profile(), reserve_ratio=0.0)
    assert policy.reserve == 0 and policy.target == policy.headroom


def test_public_view_reports_every_number_a_job_needs_to_be_audited():
    view = static_archive_policy(_profile()).public_view()
    assert set(view) == {
        "input_limit", "trigger", "headroom", "reserve", "target",
        "trigger_ratio", "reserve_ratio", "version", "min_turns",
    }


# --------------------------------------------------------------------------
# The third term of R
# --------------------------------------------------------------------------

def test_hot_window_exposes_the_static_policy_it_derived():
    window = hot_window(_profile())
    assert (window.archive_trigger, window.archive_target) == (7004, 11675)
    assert window.archive_policy_version == ARCHIVE_POLICY_VERSION
    assert window.static_policy().target == 11675


def test_the_third_term_binds_once_the_prefix_is_large_enough():
    window = hot_window(_profile())
    # min(ceiling=16384, 0.25*H=5837, T - P). At P = 0 the ratio binds.
    assert window.recent_window_for(0) == 5837
    # Past T - 5837 the static term takes over and tightens R.
    assert window.recent_window_for(7000) == 11675 - 7000


def test_the_third_term_is_inert_at_the_repository_profile():
    """Honest finding, pinned so it cannot silently change.

    At H = 23348 the ratio term (5837) is far below T (11675), so the static
    term only starts to bind when P exceeds 5838 bytes. The measured adapter
    envelope is ~143, so wiring T in does not change today's R -- it completes
    the formula for profiles where the ratio term is larger.
    """
    window = hot_window(_profile())
    assert window.recent_window_bytes == 5837
    assert window.recent_window_for(window.envelope_for(1)) == 5837
    assert window.envelope_for(1) < 5838


def test_envelope_for_is_the_difference_packing_limit_subtracts():
    window = hot_window(_profile())
    assert window.envelope_for(7, 2) == window.input_limit - window.packing_limit(7, 2)


def test_a_declared_prefix_reserve_flows_into_the_policy_report():
    window = hot_window(_profile(archive_prefix_reserve=4096))
    assert window.archive_prefix_reserve == 4096


# --------------------------------------------------------------------------
# The policy survives a database round trip
# --------------------------------------------------------------------------

def test_the_declared_line_survives_a_round_trip_through_the_profile_version(tmp_path):
    """A policy that cannot round-trip the database is a default with extra steps."""
    from app.model_admin import ModelAdminService
    from app.model_control import RoutedModelGateway

    db = Database(tmp_path / "roundtrip.db")
    admin = ModelAdminService(db)
    declared = _profile(
        archive_trigger_ratio=0.1, archive_reserve_ratio=0.4, archive_prefix_reserve=2_048,
    )
    registered = admin.ensure_profile(declared)
    version_id = registered.registered_profile_version_id

    reported = admin.version(version_id)
    assert reported["archive_trigger_ratio"] == 0.1
    assert reported["archive_reserve_ratio"] == 0.4
    assert reported["archive_prefix_reserve"] == 2_048

    # And the profile the gateway would actually route to carries them.
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM model_profile_versions WHERE id=?", (version_id,)
        ).fetchone()
    loaded = RoutedModelGateway._profile(row)
    window = hot_window(loaded)
    assert window.archive_trigger_ratio == 0.1
    assert window.archive_reserve_ratio == 0.4
    assert window.archive_prefix_reserve == 2_048
    # The declared line is genuinely different from the defaults.
    assert window.archive_trigger == 2334
    assert window.archive_trigger != hot_window(_profile()).archive_trigger


def test_a_round_trip_that_leaves_the_knobs_null_keeps_the_defaults(tmp_path):
    from app.model_admin import ModelAdminService
    from app.model_control import RoutedModelGateway

    db = Database(tmp_path / "defaults.db")
    registered = ModelAdminService(db).ensure_profile(_profile())
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM model_profile_versions WHERE id=?",
            (registered.registered_profile_version_id,),
        ).fetchone()
    loaded = RoutedModelGateway._profile(row)
    assert loaded.archive_trigger_ratio is None
    window = hot_window(loaded)
    assert window.archive_trigger == 7004
    assert window.archive_policy_version == ARCHIVE_POLICY_VERSION


def test_the_environment_path_accepts_the_three_knobs(tmp_path, monkeypatch):
    from app.config import load_model_profile_from_env

    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENT_MODEL_ID", "deepseek-flash")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "configured")
    monkeypatch.setenv("AGENT_MODEL_ARCHIVE_TRIGGER_RATIO", "0.1")
    monkeypatch.setenv("AGENT_MODEL_ARCHIVE_RESERVE_RATIO", "0.4")
    monkeypatch.setenv("AGENT_MODEL_ARCHIVE_PREFIX_RESERVE", "2048")
    profile = load_model_profile_from_env()
    assert (profile.archive_trigger_ratio, profile.archive_reserve_ratio) == (0.1, 0.4)
    assert profile.archive_prefix_reserve == 2048


def test_an_invalid_reserve_ratio_is_rejected_by_the_admin_api(tmp_path):
    from app.model_admin import ModelAdminError, ModelAdminService

    db = Database(tmp_path / "invalid.db")
    admin = ModelAdminService(db)
    with pytest.raises(ModelAdminError):
        admin.create_profile({
            "name": "p", "provider_protocol": "openai_compatible", "provider_name": "n",
            "base_url": "https://api.example.com/v1", "model_name": "m",
            "credential_env_ref": "K", "capabilities": {"text": True},
            "context_window": 32768, "max_output_tokens": 4096,
            "archive_reserve_ratio": 1.0,
        })


# --------------------------------------------------------------------------
# Trigger evaluation against a stable snapshot
# --------------------------------------------------------------------------

def _thread_with_turns(tmp_path, sizes, *, owner_id="owner-a"):
    db = Database(tmp_path / "static.db")
    from app.conversation import ConversationService

    thread = ConversationService(db).create_thread(owner_id=owner_id)
    now = "2026-01-01T00:00:00+00:00"
    with db.transaction() as connection:
        for index, size in enumerate(sizes):
            turn_id = f"turn-{index}"
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED',?,?)", (turn_id, thread.id, turn_id, now, now),
            )
            connection.execute(
                "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                "content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                (f"msg-{index}", thread.id, turn_id, "user", "u" * size, size, index * 2 + 1, now, now),
            )
            connection.execute(
                "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                "content_length,message_seq,created_at,completed_at) VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                (f"msg-{index}-a", thread.id, turn_id, "assistant", "a" * size, size, index * 2 + 2, now, now),
            )
    archiver = ConversationArchiver(db, MemoryStore(db, tmp_path / "memory"), None)
    return db, thread, archiver


def _per_turn_cost(archiver, db, thread_id, index):
    from app.token_budget import DEFAULT_TOKEN_COUNTER

    transcript = archiver.transcripts.build(thread_id)
    turn = transcript.turns[index]
    return DEFAULT_TOKEN_COUNTER.count_text(
        json.dumps(turn.canonical(), ensure_ascii=False, sort_keys=True)
    )


def test_uncovered_cost_measures_the_unarchived_prefix(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [400, 400, 400])
    whole = archiver.transcripts.measure(archiver.transcripts.build(thread.id))
    assert archiver.uncovered_cost(thread.id) == whole
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq) VALUES (?,?,?)",
            ("owner-a", thread.id, 1),
        )
    assert archiver.uncovered_cost(thread.id) < whole


def _synthetic_policy(trigger, target, *, version="test-policy", min_turns=0):
    return StaticArchivePolicy(
        input_limit=10 ** 7, trigger=trigger, headroom=10 ** 6, reserve=0, target=target,
        trigger_ratio=0.3, reserve_ratio=0.2, version=version, min_turns=min_turns,
    )


def test_trigger_state_distinguishes_reached_from_actionable(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [200] * 6)
    costs = [_per_turn_cost(archiver, db, thread.id, index) for index in range(6)]
    # A synthetic line lets each band be hit exactly instead of by luck.
    per_turn = sum(costs[:2])

    below = archiver.static_trigger_state(thread.id, _synthetic_policy(10 ** 7, 10 ** 7))
    assert below["reached"] is False and below["actionable"] is False

    band = archiver.static_trigger_state(thread.id, _synthetic_policy(per_turn, 10 ** 7))
    assert band["reached"] is True and band["actionable"] is False

    over = archiver.static_trigger_state(thread.id, _synthetic_policy(per_turn, per_turn))
    assert over["reached"] is True and over["actionable"] is True


def test_pending_is_measured_from_committed_coverage_not_from_in_flight_work(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [500] * 4)
    before = archiver.uncovered_cost(thread.id)
    # A job that is queued but not committed must not make the prefix look smaller.
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_archive_state(owner_id,thread_id,archived_through_seq) VALUES (?,?,0)",
            ("owner-a", thread.id),
        )
    assert archiver.uncovered_cost(thread.id) == before


# --------------------------------------------------------------------------
# Enqueue gating and idempotency
# --------------------------------------------------------------------------

def test_proactive_enqueue_below_the_line_creates_no_job(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [200] * 4)
    job = archiver.enqueue(
        thread.id, proactive=True, source_turn_id="turn-3",
        static_policy=_synthetic_policy(10 ** 9, 10 ** 9),
    )
    assert job is None
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_archive_jobs").fetchone()[0] == 0


def test_proactive_enqueue_in_the_no_op_band_creates_no_job(tmp_path):
    """Between the trigger line and the target a pass can reduce nothing."""
    db, thread, archiver = _thread_with_turns(tmp_path, [200] * 4)
    whole = archiver.transcripts.measure(archiver.transcripts.build(thread.id))
    job = archiver.enqueue(
        thread.id, proactive=True, source_turn_id="turn-3",
        static_policy=_synthetic_policy(1, whole + 1000),
    )
    assert job is None
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_archive_jobs").fetchone()[0] == 0


def test_proactive_enqueue_above_the_line_creates_a_job_that_stops_at_the_target(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [1500] * 6)
    # One pass can only reach the target when the summariser's own input budget
    # is not the binding constraint; that is pinned separately below.
    archiver.max_summary_tokens = 10 ** 7
    costs = [_per_turn_cost(archiver, db, thread.id, index) for index in range(6)]
    # A target that holds exactly the two newest turns, so the assertion is
    # exact rather than approximate.
    target = costs[-1] + costs[-2] + 10
    job = archiver.enqueue(
        thread.id, proactive=True, source_turn_id="turn-5",
        static_policy=_synthetic_policy(1, target, version="test-policy", min_turns=2),
    )
    assert job is not None
    with db.connection() as connection:
        row = connection.execute(
            "SELECT budget_policy_version,end_message_seq FROM memory_archive_jobs WHERE id=?", (job,)
        ).fetchone()
    assert row["budget_policy_version"] == "test-policy"
    # The turns that stay behind must fit the static target.
    kept = archiver.transcripts.build(thread.id, after_sequence=row["end_message_seq"])
    assert archiver.transcripts.measure(kept) <= target


def test_a_pass_bounded_by_the_summariser_budget_still_makes_progress(tmp_path):
    """The target is a goal, not a guarantee.

    One pass is also bounded by the summariser's input budget, so a single job
    may stop short of the target. That is not a failure: the prefix is smaller
    than it was, the line is still crossed, and the next pass continues. What
    must never happen is a pass that archives nothing and re-enqueues forever.
    """
    db, thread, archiver = _thread_with_turns(tmp_path, [1500] * 6)
    whole = archiver.transcripts.measure(archiver.transcripts.build(thread.id))
    target = whole // 6  # far below what one summariser-bounded batch can reach
    job = archiver.enqueue(
        thread.id, proactive=True, source_turn_id="turn-5",
        static_policy=_synthetic_policy(1, target, min_turns=1),
    )
    assert job is not None
    with db.connection() as connection:
        end = connection.execute(
            "SELECT end_message_seq FROM memory_archive_jobs WHERE id=?", (job,)
        ).fetchone()["end_message_seq"]
    remaining = archiver.transcripts.measure(
        archiver.transcripts.build(thread.id, after_sequence=end)
    )
    assert 0 < remaining < whole


def test_the_static_target_never_archives_into_the_newest_min_turns(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [1500] * 6)
    whole = archiver.transcripts.measure(archiver.transcripts.build(thread.id))
    job = archiver.enqueue(
        thread.id, proactive=True, source_turn_id="turn-5",
        static_policy=_synthetic_policy(1, 0, min_turns=3),
    )
    assert job is not None
    with db.connection() as connection:
        end = connection.execute(
            "SELECT end_message_seq FROM memory_archive_jobs WHERE id=?", (job,)
        ).fetchone()["end_message_seq"]
    remaining = archiver.transcripts.build(thread.id, after_sequence=end)
    assert len(remaining.turns) >= 3


def test_the_gate_never_suppresses_a_foreground_enqueue(tmp_path):
    """The foreground is already reacting to a request that does not fit."""
    db, thread, archiver = _thread_with_turns(tmp_path, [1500] * 4)
    whole = archiver.transcripts.measure(archiver.transcripts.build(thread.id))
    job = archiver.enqueue(
        thread.id, source_turn_id="turn-3",
        # Trigger line far above the prefix, i.e. the gate would say "no".
        static_policy=_synthetic_policy(10 ** 9, whole // 2),
    )
    assert job is not None


def test_no_policy_keeps_the_legacy_unconditional_behaviour(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [200] * 4)
    archiver.keep_tokens = 1
    assert archiver.enqueue(thread.id, proactive=True, source_turn_id="turn-3") is not None


def test_the_same_snapshot_cannot_produce_two_jobs(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [1500] * 6)
    whole = archiver.transcripts.measure(archiver.transcripts.build(thread.id))
    policy = _synthetic_policy(1, whole // 3)
    first = archiver.enqueue(thread.id, proactive=True, source_turn_id="turn-5", static_policy=policy)
    second = archiver.enqueue(thread.id, proactive=True, source_turn_id="turn-5", static_policy=policy)
    assert first is not None and second == first
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_archive_jobs").fetchone()[0] == 1


def test_a_foreground_job_records_the_profile_version_but_no_policy_version(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [1500] * 4)
    job = archiver.enqueue(thread.id, source_turn_id="turn-3", budget_profile_version_id="mpv-1")
    with db.connection() as connection:
        row = connection.execute(
            "SELECT budget_policy_version,budget_profile_version_id FROM memory_archive_jobs WHERE id=?",
            (job,),
        ).fetchone()
    # NULL policy version is accurate: this pass was forced by the hard cap.
    assert row["budget_policy_version"] is None
    assert row["budget_profile_version_id"] == "mpv-1"


# --------------------------------------------------------------------------
# Background limits
# --------------------------------------------------------------------------

def test_rate_cap_bounds_how_many_passes_start_per_minute(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [200])
    worker = ManagedArchiveWorker(archiver, max_jobs_per_minute=2)
    assert worker._rate_allows() is True
    worker._recent_jobs.extend([time.monotonic(), time.monotonic()])
    assert worker._rate_allows() is False
    worker._recent_jobs.clear()
    worker._recent_jobs.extend([time.monotonic() - 61, time.monotonic()])
    assert worker._rate_allows() is True


def test_rate_cap_rejects_a_non_positive_limit(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [200])
    with pytest.raises(ValueError):
        ManagedArchiveWorker(archiver, max_jobs_per_minute=0)


def test_the_rate_cap_is_configured_from_the_environment(monkeypatch):
    from app.memory_archive import archive_jobs_per_minute_from_env

    monkeypatch.delenv("AGENT_ARCHIVE_MAX_JOBS_PER_MINUTE", raising=False)
    assert archive_jobs_per_minute_from_env() is None
    monkeypatch.setenv("AGENT_ARCHIVE_MAX_JOBS_PER_MINUTE", "6")
    assert archive_jobs_per_minute_from_env() == 6
    monkeypatch.setenv("AGENT_ARCHIVE_MAX_JOBS_PER_MINUTE", "0")
    with pytest.raises(ValueError):
        archive_jobs_per_minute_from_env()
    monkeypatch.setenv("AGENT_ARCHIVE_MAX_JOBS_PER_MINUTE", "soon")
    with pytest.raises(ValueError):
        archive_jobs_per_minute_from_env()


def test_the_worker_yields_between_jobs_instead_of_starving_the_foreground(tmp_path):
    """One job per call: the loop can be pre-empted between passes."""
    db, thread, archiver = _thread_with_turns(tmp_path, [200])
    worker = ManagedArchiveWorker(archiver)
    assert worker.foreground_probe is None
    assert worker._foreground_busy() is False


@pytest.mark.asyncio
async def test_a_queued_foreground_turn_stops_the_worker_touching_anything(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [200])
    archiver.keep_tokens = 1
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO turn_jobs(turn_id,status) VALUES ('turn-0','QUEUED')",
        )
    worker = ManagedArchiveWorker(archiver, foreground_probe=lambda: foreground_turn_pending(db))
    assert await worker.run_once() is False
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_archive_jobs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_a_broken_foreground_probe_does_not_stall_archival(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [200])
    archiver.keep_tokens = 1

    async def summary(payload):
        ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
        return {
            "synopsis": [{"text": "s", "source_message_ids": [ids[0]]}],
            "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
        }

    archiver.summarizer = summary
    assert archiver.enqueue(thread.id) is not None

    def broken():
        raise RuntimeError("probe is unavailable")

    worker = ManagedArchiveWorker(archiver, foreground_probe=broken)
    assert await worker.run_once() is True
    assert archiver.status(thread.id, "owner-a")["archived_through_seq"] > 0


def test_foreground_turn_pending_only_counts_waiting_turns(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [200])
    assert foreground_turn_pending(db) is False
    with db.transaction() as connection:
        connection.execute("INSERT INTO turn_jobs(turn_id,status) VALUES ('turn-0','RUNNING')")
    # A turn in flight is not a turn waiting: treating it as foreground activity
    # would stop archival entirely on a busy system.
    assert foreground_turn_pending(db) is False
    with db.transaction() as connection:
        connection.execute("INSERT INTO turn_jobs(turn_id,status) VALUES ('turn-1','QUEUED')")
    assert foreground_turn_pending(db) is True


@pytest.mark.asyncio
async def test_a_signal_below_the_line_is_spent_not_retried_forever(tmp_path):
    """No queue storm: a signal that crosses nothing must not be re-evaluated."""
    db, thread, archiver = _thread_with_turns(tmp_path, [200] * 4)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_archive_signals(turn_id,thread_id,created_at) VALUES ('turn-3',?,?)",
            (thread.id, "2026-01-01T00:00:00+00:00"),
        )
    worker = ManagedArchiveWorker(archiver)
    worker.archiver.static_policy_and_version_for_turn = lambda *a, **k: (
        _synthetic_policy(10 ** 9, 10 ** 9), None,
    )
    assert await worker.run_once() is True
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_archive_signals").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_archive_jobs").fetchone()[0] == 0
    # Nothing is left to do, so the next poll makes no progress at all.
    assert await worker.run_once() is False


# --------------------------------------------------------------------------
# A background completion cannot change a prompt that was already sent
# --------------------------------------------------------------------------

def test_a_running_background_job_changes_neither_the_render_nor_the_line(tmp_path):
    """A pass in flight is invisible to the hot path.

    The selector reads the *committed* coverage cursor. A job holding a lease
    therefore cannot make a fitting request wait, cannot shrink what the
    selector renders, and cannot move the static line.
    """
    from datetime import datetime, timedelta, timezone

    from app.transcript import CanonicalTurnTranscriptBuilder

    db, thread, archiver = _thread_with_turns(tmp_path, [400] * 6)
    builder = CanonicalTurnTranscriptBuilder(db)
    before = json.dumps(builder.render_history(builder.build(thread.id)), ensure_ascii=False)
    pending_before = archiver.uncovered_cost(thread.id)
    line_before = archiver.static_trigger_state(thread.id, static_archive_policy(_profile()))

    lease = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_archive_jobs(id,owner_id,thread_id,start_message_seq,end_message_seq,"
            "source_hash,prompt_version,tokenizer_version,status,available_at,max_attempts,created_at,"
            "updated_at,lease_owner,lease_epoch,lease_until) "
            "VALUES ('running-1','owner-a',?,1,2,'sha256:x','episode-v4','utf8-upper-bound-v1','RUNNING',"
            "'2026-01-01T00:00:00+00:00',3,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','w',1,?)",
            (thread.id, lease),
        )

    assert json.dumps(builder.render_history(builder.build(thread.id)), ensure_ascii=False) == before
    assert archiver.uncovered_cost(thread.id) == pending_before
    assert archiver.static_trigger_state(thread.id, static_archive_policy(_profile())) == line_before


def test_no_reachable_profile_means_no_invented_line(tmp_path):
    """Without a routed profile there is no defensible line, so none is made up."""
    db, thread, archiver = _thread_with_turns(tmp_path, [400] * 2)
    assert archiver.summarizer is None
    assert archiver.static_policy_and_version_for_turn(thread.id, "turn-1") == (None, None)
    assert archiver.static_policy_for_turn(thread.id, "turn-1") is None


@pytest.mark.asyncio
async def test_a_late_commit_does_not_mutate_an_already_rendered_prompt(tmp_path):
    db, thread, archiver = _thread_with_turns(tmp_path, [400] * 6)

    async def summary(payload):
        ids = [event["message_id"] for turn in payload["turns"] for event in turn["events"] if event["message_id"]]
        return {
            "synopsis": [{"text": "s", "source_message_ids": [ids[0]]}],
            "topics": [], "decisions": [], "outcomes": [], "open_loops": [], "sensitivity": "normal",
        }

    archiver.summarizer = summary
    archiver.keep_tokens = 1

    from app.transcript import CanonicalTurnTranscriptBuilder

    builder = CanonicalTurnTranscriptBuilder(db)
    rendered = builder.render_history(builder.build(thread.id))
    frozen = json.dumps(rendered, ensure_ascii=False)

    job = archiver.enqueue(thread.id)
    assert job is not None
    assert await archiver.process(archiver.claim(job)) is not None

    # The prompt that was already built is byte-identical; the archive landed
    # behind it and only affects the *next* render.
    assert json.dumps(rendered, ensure_ascii=False) == frozen
    assert archiver.status(thread.id, "owner-a")["archived_through_seq"] > 0
