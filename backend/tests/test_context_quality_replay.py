"""R1 hard acceptance: the quality replay (M3-01).

Retained bytes, archive counts, first-token latency and cost cannot show that a
compaction change is *better*. This module pins the only verdict that counts:

* the uncompressed reference is a valid upper bound (otherwise no comparison
  means anything);
* post-R1 never loses a probe that pre-R1 kept;
* post-R1 never falls below the uncompressed reference;
* the summary outliving the raw history it replaces is what actually carries the
  old facts - the P0 the M2-02 fix addresses;
* ``min_turns`` choice never degrades quality.
"""

import pytest

from app.context_replay import (
    PROBES,
    VARIANTS,
    assemble,
    evaluate,
    run_probes,
)


def _passed(messages) -> int:
    return sum(1 for item in run_probes(messages) if item["passed"])


def test_reference_is_a_valid_upper_bound() -> None:
    """If the uncompressed reference fails, the fixture is broken, not the code."""
    messages = assemble(**VARIANTS["reference"])

    assert _passed(messages) == len(PROBES)


def test_pre_r1_loses_the_facts_that_post_r1_keeps() -> None:
    """The decisive comparison: same budget, only the priority differs."""
    pre = _passed(assemble(**VARIANTS["pre_r1"]))
    post = _passed(assemble(**VARIANTS["post_r1"]))

    assert pre < len(PROBES), "the fixture must actually exercise the priority ladder"
    assert post == len(PROBES)
    assert post > pre


def test_summary_must_outrank_the_raw_history_it_replaces() -> None:
    """At priority 20 the summary is dropped before the history it summarises."""
    pre = {item["probe_id"]: item for item in run_probes(assemble(**VARIANTS["pre_r1"]))}
    post = {item["probe_id"]: item for item in run_probes(assemble(**VARIANTS["post_r1"]))}

    archived_facts = [probe.probe_id for probe in PROBES if probe.probe_id != "recent_turns"]
    for probe_id in archived_facts:
        assert pre[probe_id]["passed"] is False, (
            f"{probe_id} should be lost when the summary is dropped first"
        )
        assert post[probe_id]["passed"] is True, (
            f"{probe_id} should survive once the summary outranks raw history"
        )
    # The recent-turns channel is independent of the summary and must work in both.
    assert pre["recent_turns"]["passed"] is True
    assert post["recent_turns"]["passed"] is True


def test_a_superseded_value_must_not_outlive_its_correction() -> None:
    """Recency resolution, not mere absence, is the standard."""
    messages = assemble(**VARIANTS["post_r1"])
    result = {item["probe_id"]: item for item in run_probes(messages)}["latest_value"]

    assert result["retained"] is True
    assert result["contaminated"] == []


@pytest.mark.parametrize("min_turns", [0, 1, 3, 5, 8, 12])
def test_min_turns_never_degrades_quality(min_turns: int) -> None:
    messages = assemble(summary_priority=60, budget=23_348, min_turns=min_turns)

    assert _passed(messages) == len(PROBES)


def test_replay_verdict_passes() -> None:
    verdict = evaluate()["verdict"]

    assert verdict["passed"] is True, verdict["reason"]
    assert verdict["improved"] > 0
