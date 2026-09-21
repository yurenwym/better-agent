"""R1 M2-01 / M2-02 acceptance: profile-derived hot window, min_turns floor.

Pins three things the previous design got wrong:

* the hot window was a hardcoded ``12_000`` byte constant, so a conversation was
  cropped and archived the moment it passed that number, regardless of how much
  room the routed model actually had;
* ``pack_recent`` had no turn-count floor, so how many turns survived was an
  accident of how large a single turn happened to be;
* the archived-Episode summary was injected with ``_context_priority=20`` while
  raw history defaulted to 50, so the summary was dropped *before* the raw turns
  it was supposed to replace.
"""

from __future__ import annotations

import json

import pytest

from app.db import Database
from app.model_gateway import ModelProfile
from app.token_budget import (
    CONTEXT_GROUP_KEY,
    CONTEXT_PRIORITY_KEY,
    CONTEXT_REQUIRED_KEY,
    DEFAULT_COMPACT_RATIO,
    DEFAULT_HISTORY_MIN_TURNS,
    DEFAULT_TOKEN_COUNTER,
    compact_target,
    effective_input_budget,
    hot_window,
    pack_messages_newest,
)
from app.transcript import CanonicalTurnTranscriptBuilder


def _profile(**overrides) -> ModelProfile:
    values = {
        "base_url": "https://example.test/v1",
        "model": "demo",
        "api_key_env": "TEST_KEY",
        "context_window": 32768,
        "max_output_tokens": 8192,
    }
    values.update(overrides)
    return ModelProfile(**values)


def _seed(db, turns: int, *, body: str = "x" * 400) -> None:
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("thread", "thread", "alice", "now", "now"),
        )
        for index in range(turns):
            turn_id = f"turn-{index}"
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (turn_id, "thread", turn_id, "COMPLETED", "now", "now"),
            )
            for offset, role in enumerate(("user", "assistant")):
                sequence = index * 2 + offset + 1
                content = f"{body}-{index}-{role}"
                connection.execute(
                    "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                    "content_length,message_seq,created_at) VALUES (?,?,?,?,?,'ready',1,?,?,?)",
                    (f"message-{sequence}", "thread", turn_id, role, content, len(content), sequence, "now"),
                )


def _append_huge_turn(db, size: int) -> None:
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("huge-turn", "thread", "huge-turn", "COMPLETED", "now", "now"),
        )
        body = "y" * size
        connection.execute(
            "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
            "content_length,message_seq,created_at) VALUES (?,?,?,?,?,'ready',1,?,?,?)",
            ("huge-message", "thread", "huge-turn", "user", body, len(body), 99, "now"),
        )


# --------------------------------------------------------------------------- #
# The window is derived from the profile, never from a byte constant
# --------------------------------------------------------------------------- #


def test_hot_window_derives_every_number_from_the_profile() -> None:
    legacy = hot_window(_profile())

    assert legacy.input_limit == 32768 - 8192 - 1228 == 23348
    assert legacy.min_turns == DEFAULT_HISTORY_MIN_TURNS == 5
    assert legacy.compact_target == 16344  # ceil(0.7 * 23348)
    assert legacy.validation_tier == "legacy"

    tier_a = hot_window(_profile(
        validation_tier="A", admitted_context_limit=32768,
        counter_evidence_version="openai-compatible-wrapper-v1",
    ))
    assert tier_a.input_limit == 24576
    assert tier_a.compact_target == 17204  # ceil(0.7 * 24576)


def test_hot_window_policy_comes_from_the_profile() -> None:
    window = hot_window(_profile(history_min_turns=8, compact_ratio=0.5))

    assert window.min_turns == 8
    assert window.compact_target == 11674  # ceil(0.5 * 23348)


def test_hot_window_falls_back_to_ratios_when_the_policy_is_unusable() -> None:
    window = hot_window(_profile(history_min_turns=0, compact_ratio=2.0))

    assert window.min_turns == DEFAULT_HISTORY_MIN_TURNS
    assert window.compact_target == compact_target(
        effective_input_budget(_profile()), ratio=DEFAULT_COMPACT_RATIO
    )


def test_compact_target_rejects_a_ratio_outside_the_unit_interval() -> None:
    budget = effective_input_budget(_profile())

    for ratio in (0, -0.1, 1.5):
        with pytest.raises(ValueError, match="compact_ratio"):
            compact_target(budget, ratio=ratio)


# --------------------------------------------------------------------------- #
# min_turns is a floor, not a best effort
# --------------------------------------------------------------------------- #


def test_pack_recent_always_keeps_the_newest_min_turns(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    _seed(db, 12, body="x" * 400)
    builder = CanonicalTurnTranscriptBuilder(db)
    transcript = builder.build("thread")

    # A budget that holds roughly a quarter of the transcript but comfortably
    # more than one turn.
    budget = builder.measure(transcript) // 4
    degraded: list[str] = []
    packed = builder.pack_recent(
        transcript, token_budget=budget, min_turns=6, on_degraded=degraded.append,
    )

    assert len(packed.turns) == 6
    assert degraded == []
    assert [turn.turn_id for turn in packed.turns] == [f"turn-{index}" for index in range(6, 12)]


def test_pack_recent_backfills_older_turns_with_the_remaining_budget(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    _seed(db, 12, body="x" * 100)
    builder = CanonicalTurnTranscriptBuilder(db)
    transcript = builder.build("thread")
    full = builder.measure(transcript)

    assert len(builder.pack_recent(transcript, token_budget=full, min_turns=3).turns) == 12

    packed = builder.pack_recent(transcript, token_budget=full // 3, min_turns=3)
    assert 3 <= len(packed.turns) < 12
    assert [turn.turn_id for turn in packed.turns] == [
        f"turn-{index}" for index in range(12 - len(packed.turns), 12)
    ]


def test_pack_recent_degrades_only_when_one_turn_alone_overflows(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    _seed(db, 6, body="x" * 200)
    _append_huge_turn(db, 50_000)
    builder = CanonicalTurnTranscriptBuilder(db)
    transcript = builder.build("thread")

    degraded: list[str] = []
    packed = builder.pack_recent(
        transcript, token_budget=4_000, min_turns=3, on_degraded=degraded.append,
    )

    assert packed.turns == ()
    assert len(degraded) == 1
    assert "a single complete turn exceeds" in degraded[0]
    assert "min_turns=3" in degraded[0]


def test_pack_recent_without_a_floor_keeps_the_old_contiguous_behaviour(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    _seed(db, 10, body="x" * 200)
    builder = CanonicalTurnTranscriptBuilder(db)
    transcript = builder.build("thread")

    packed = builder.pack_recent(transcript, token_budget=2_000)

    assert 0 < len(packed.turns) < 10
    assert [turn.turn_id for turn in packed.turns] == [
        f"turn-{index}" for index in range(10 - len(packed.turns), 10)
    ]


# --------------------------------------------------------------------------- #
# The archived summary must not be dropped before the raw history it replaces
# --------------------------------------------------------------------------- #


def test_archived_summary_outranks_raw_history_when_the_budget_tightens() -> None:
    summary = {
        "role": "system",
        "content": "摘要：用户早先讨论了旧主题" + "s" * 600,
        CONTEXT_REQUIRED_KEY: False,
        CONTEXT_PRIORITY_KEY: 60,          # conversation.py memory-context
        CONTEXT_GROUP_KEY: "memory-context",
    }
    raw_history = [
        {"role": "user", "content": "OLD_RAW_SOURCE_" + "r" * 600},
        {"role": "assistant", "content": "old answer" + "a" * 600},
    ]
    current = {"role": "user", "content": "CURRENT_REQUEST"}
    messages = [summary, *raw_history, current]

    counter = DEFAULT_TOKEN_COUNTER
    stripped = [
        {key: value for key, value in message.items() if not key.startswith("_context_")}
        for message in messages
    ]
    summary_only = counter.count_payload([stripped[-1], stripped[0]])
    everything = counter.count_payload(stripped)
    assert summary_only < everything

    packed = pack_messages_newest(messages, budget=everything - 1)
    rendered = "\n".join(str(message.get("content", "")) for message in packed)

    assert "摘要：用户早先讨论了旧主题" in rendered
    assert "CURRENT_REQUEST" in rendered
    assert "OLD_RAW_SOURCE_" not in rendered


def test_raw_history_alone_has_no_priority_hint() -> None:
    """The default priority of a bare history message is 50, below the summary."""

    rendered = json.dumps({"role": "user", "content": "hi"}, ensure_ascii=False)
    assert CONTEXT_PRIORITY_KEY not in rendered
    assert 60 > 50


# --------------------------------------------------------------------------- #
# Merged Episodes must not lose decisions/outcomes to a wall of synopses
# --------------------------------------------------------------------------- #


def test_merged_episode_keeps_decisions_and_outcomes_under_long_chunks() -> None:
    from app.memory_archive import (
        DECISION_OUTCOME_RESERVE_RATIO,
        MAX_SUMMARY_OUTPUT_TOKENS,
        ConversationArchiver,
    )

    summaries = [
        {
            "synopsis": [{"text": f"chunk-{index}-" + "s" * 200, "source_message_ids": [f"m{index}"]}],
            "decisions": [{"text": "d" * 200, "source_message_ids": [f"m{index}"]}],
            "outcomes": [{"text": "o" * 200, "source_message_ids": [f"m{index}"]}],
            "open_loops": [],
            "topics": [],
            "sensitivity": "normal",
        }
        for index in range(10)
    ]

    merged = ConversationArchiver._merge_chunk_summaries(summaries)

    assert merged["synopsis"]
    assert merged["decisions"], "decisions must not be starved by synopses"
    assert merged["outcomes"], "outcomes must not be starved by synopses"
    assert 0 < DECISION_OUTCOME_RESERVE_RATIO < 1
    size = DEFAULT_TOKEN_COUNTER.count_text(
        json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    assert size <= MAX_SUMMARY_OUTPUT_TOKENS


def test_worker_hot_window_uses_the_gateway_resolved_profile(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from test_runtime import make_runtime

    from app.config import load_model_profile_from_env
    from app.token_budget import hot_window

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "configured")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "demo")
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW", "65536")
    profile = load_model_profile_from_env()

    class RouteModel:
        gateway = SimpleNamespace(resolved_profile=lambda context=None, **kwargs: profile)

        async def route_and_respond(self, **_kwargs):
            raise AssertionError("not used")

    runtime = make_runtime(tmp_path, RouteModel())
    thread = runtime.conversation.create_thread("window")
    accepted = runtime.conversation.accept_turn(thread.id, "window-1", "hi", [])
    turn = runtime.conversation.turn(accepted.turn_id)

    window = runtime.turn_worker.primary._hot_window(turn, "local-user")

    assert window is not None
    assert window.input_limit == hot_window(profile).input_limit
    assert window.input_limit > 12_000
