"""R2-02: the recent window has two constraints, and they do different jobs.

``N`` (``min_turns``) is a floor on the **count**: the newest few complete turns
are a property of the window, not a best effort, so how many survive stops
depending on how large one turn happens to be.

``R`` (``recent_window_budget``) is a ceiling on the **bytes**: without it a long
recent conversation monopolises the whole budget and crowds out the summary,
which is the channel that carries the older facts.

The plan names two things that must not happen, and both are pinned here:

* the newest large turn must never be skipped to backfill an older smaller one;
* when the whole transcript fits, ``R`` must not trim original text.
"""

from __future__ import annotations

import json

import pytest

from app.token_budget import (
    DEFAULT_RECENT_WINDOW_BYTES,
    DEFAULT_RECENT_WINDOW_RATIO,
    DEFAULT_TOKEN_COUNTER,
    EffectiveInputBudget,
    hot_window,
    recent_window_budget,
    recent_window_ceiling,
    recent_window_ratio,
)
from app.transcript import (
    CanonicalTranscript,
    CanonicalTurnTranscriptBuilder,
    ResolvedMemoryScope,
    TranscriptEvent,
    TranscriptTurn,
    _source_hash,
)


def _turn(index: int, *, content: str = "内容") -> TranscriptTurn:
    events = (
        TranscriptEvent("message", f"turn-{index}", f"m{index}-1", None, "user", content, index * 2),
        TranscriptEvent("message", f"turn-{index}", f"m{index}-2", None, "assistant", "好的。", index * 2 + 1),
    )
    return TranscriptTurn(f"turn-{index}", "completed", index * 2, index * 2 + 1, events)


def _transcript(turns: list[TranscriptTurn]) -> CanonicalTranscript:
    scope = ResolvedMemoryScope(owner_id="local-user", thread_id="t", project_id=None)
    return CanonicalTranscript(scope, tuple(turns), _source_hash(scope, turns, ()), ())


def _cost(turn: TranscriptTurn) -> int:
    return DEFAULT_TOKEN_COUNTER.count_text(json.dumps(turn.canonical(), ensure_ascii=False, sort_keys=True))


def _pack(transcript, budget, *, min_turns=0, recent_budget=None):
    return CanonicalTurnTranscriptBuilder.pack_recent(
        None, transcript, budget, min_turns=min_turns, recent_budget=recent_budget,
    )


# --------------------------------------------------------------------------- #
# The R formula
# --------------------------------------------------------------------------- #


def test_recent_window_budget_is_the_minimum_of_its_bounds():
    """``R = min(ceiling, floor(ratio * H))`` while R2-03 is off."""
    assert recent_window_budget(23_348) == min(DEFAULT_RECENT_WINDOW_BYTES, int(0.25 * 23_348))


def test_recent_window_budget_scales_with_the_model():
    """The ratio term dominates for a 32k model; the ceiling only caps big ones."""
    assert recent_window_budget(8_000) == 2_000
    assert recent_window_budget(1_000_000) == DEFAULT_RECENT_WINDOW_BYTES


def test_recent_window_budget_honours_the_static_target_term():
    """Once R2-03 supplies ``T`` and ``P``, ``max(0, T - P)`` can bind."""
    assert recent_window_budget(23_348, static_target=1_000, static_prefix=200) == 800
    assert recent_window_budget(23_348, static_target=100, static_prefix=900) == 0


def test_recent_window_budget_rejects_an_out_of_range_ratio():
    with pytest.raises(ValueError):
        recent_window_budget(1_000, ratio=1.5)


def test_profile_overrides_win_over_the_defaults():
    from app.model_gateway import ModelProfile

    profile = ModelProfile(
        base_url="https://api.deepseek.com", model="m", api_key_env="K",
        context_window=32_768, max_output_tokens=8_192,
        recent_window_bytes=4_096, recent_window_ratio=0.5,
    )
    assert recent_window_ceiling(profile) == 4_096
    assert recent_window_ratio(profile) == 0.5
    window = hot_window(profile)
    assert window.recent_window_bytes == min(4_096, int(0.5 * window.input_limit))
    assert window.public_view()["recent_window_bytes"] == window.recent_window_bytes


def test_invalid_profile_knobs_fall_back_to_the_documented_defaults():
    from app.model_gateway import ModelProfile

    profile = ModelProfile(
        base_url="https://api.deepseek.com", model="m", api_key_env="K",
        recent_window_bytes=-5, recent_window_ratio=0.0,
    )
    assert recent_window_ceiling(profile) == DEFAULT_RECENT_WINDOW_BYTES
    assert recent_window_ratio(profile) == DEFAULT_RECENT_WINDOW_RATIO


# --------------------------------------------------------------------------- #
# N is a floor on the count
# --------------------------------------------------------------------------- #


def test_min_turns_is_still_honoured_when_no_recent_ceiling_is_given():
    turns = [_turn(index) for index in range(20)]
    five = sum(_cost(turn) for turn in turns[15:])
    packed = _pack(_transcript(turns), five, min_turns=5)
    assert [turn.turn_id for turn in packed.turns] == [f"turn-{index}" for index in range(15, 20)]


def test_min_turns_is_honoured_even_when_the_floor_alone_exceeds_the_budget():
    """The floor is not a best effort; it is a property of the window.

    The budget holds one turn, the floor asks for four, and four is what
    survives - otherwise how many turns survive would depend on how large one
    turn happens to be.
    """
    turns = [_turn(index, content="内容" * 200) for index in range(10)]
    packed = _pack(_transcript(turns), _cost(turns[-1]), min_turns=4)
    assert len(packed.turns) == 4


def test_a_single_turn_over_the_hard_cap_still_degrades_and_is_reported():
    turns = [_turn(index, content="内容" * 200) for index in range(10)]
    reasons: list[str] = []
    packed = CanonicalTurnTranscriptBuilder.pack_recent(
        None, _transcript(turns), 10, min_turns=4, on_degraded=reasons.append,
    )
    assert packed.turns == ()
    assert reasons and "exceeds budget" in reasons[0]


# --------------------------------------------------------------------------- #
# R releases protection from the oldest recent turn
# --------------------------------------------------------------------------- #


def test_recent_ceiling_releases_the_oldest_protected_turn():
    turns = [_turn(index, content="内容" * 50) for index in range(20)]
    cost = _cost(turns[-1])
    packed = _pack(_transcript(turns), cost * 10, min_turns=8, recent_budget=cost * 3)
    assert [turn.turn_id for turn in packed.turns] == [f"turn-{index}" for index in range(17, 20)]


def test_recent_ceiling_never_releases_the_newest_turn():
    """Even a single turn larger than R is carried - the newest is not optional."""
    turns = [_turn(index, content="内容" * 50) for index in range(5)]
    newest_cost = _cost(turns[-1])
    packed = _pack(
        _transcript(turns), newest_cost * 3, min_turns=5,
        recent_budget=max(newest_cost // 10, 1),
    )
    assert packed.turns[-1].turn_id == "turn-4"
    assert len(packed.turns) == 1


def test_release_is_reported_through_on_degraded():
    turns = [_turn(index, content="内容" * 50) for index in range(20)]
    reasons: list[str] = []
    CanonicalTurnTranscriptBuilder.pack_recent(
        None, _transcript(turns), _cost(turns[-1]) * 19,
        min_turns=8, recent_budget=_cost(turns[-1]) * 2, on_degraded=reasons.append,
    )
    assert reasons and "recent_budget" in reasons[0]


def test_no_release_happens_when_the_protected_window_already_fits():
    turns = [_turn(index) for index in range(20)]
    five = sum(_cost(turn) for turn in turns[15:])
    reasons: list[str] = []
    packed = CanonicalTurnTranscriptBuilder.pack_recent(
        None, _transcript(turns), five,
        min_turns=5, recent_budget=five, on_degraded=reasons.append,
    )
    assert [turn.turn_id for turn in packed.turns] == [f"turn-{index}" for index in range(15, 20)]
    assert reasons == []


# --------------------------------------------------------------------------- #
# The two constraints the plan names explicitly
# --------------------------------------------------------------------------- #


def test_whole_transcript_fitting_is_not_trimmed_by_the_ceiling():
    """``R`` reserves room for the summary; if everything fits, there is nothing
    to reserve and trimming would be a pure loss of original text."""
    turns = [_turn(index) for index in range(6)]
    total = sum(_cost(turn) for turn in turns)
    packed = _pack(_transcript(turns), total, min_turns=5, recent_budget=1)
    assert [turn.turn_id for turn in packed.turns] == [f"turn-{index}" for index in range(6)]


def test_a_newer_large_turn_is_not_skipped_to_backfill_an_older_smaller_one():
    """Contiguity: the suffix is a window, not a best-effort bag of turns."""
    turns = [_turn(index, content="内容" * 400) for index in range(8)]
    turns.append(_turn(8, content="内容" * 4000))   # newest turn is huge
    turns.append(_turn(9))                          # then a small one
    packed = _pack(_transcript(turns), 6_000, min_turns=2)
    kept = [turn.turn_id for turn in packed.turns]
    # Whatever survives must be a contiguous newest suffix - never turn-8 alone
    # plus turn-9 while turn-9's own predecessors are dropped out of order.
    assert kept == [f"turn-{index}" for index in range(10 - len(kept), 10)]


def test_the_recent_ceiling_bounds_the_expansion_too():
    """R must bind the expansion, not just the floor.

    Bounding only the protected suffix leaves the expansion free to refill the
    window up to the hard cap, which defeats the reservation and evicts the
    summary - the exact failure R exists to prevent.
    """
    turns = [_turn(index, content="内容" * 50) for index in range(20)]
    unit = _cost(turns[-1])
    packed = _pack(_transcript(turns), unit * 12, min_turns=10, recent_budget=unit * 3)
    assert len(packed.turns) == 3
    assert [turn.turn_id for turn in packed.turns] == ["turn-17", "turn-18", "turn-19"]
