from __future__ import annotations

import pytest


def test_m3_budget_is_bounded_before_network_call() -> None:
    """Freeze the arithmetic used by the M3 approval sheet, without I/O."""
    from app.costs import PriceSnapshot, estimate_cost
    from app.model_gateway import UsageBuckets

    price = PriceSnapshot(
        "m3-draft-price",
        uncached_input_rate=440_000,
        cache_read_rate=14_000,
        cache_write_rate=0,
        output_rate=1_320_000,
        reasoning_rate=0,
    )
    per_attempt = estimate_cost(
        UsageBuckets(32_768, 0, 0, 8_192, 0), price,
    )
    assert per_attempt.microusd == 25_232
    assert per_attempt.status == "ESTIMATED_COMPLETE"
    assert per_attempt.microusd * 15 == 378_480
    with pytest.raises(AssertionError):
        assert per_attempt.microusd * 16 <= 378_480
