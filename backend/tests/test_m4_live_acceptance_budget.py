from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _live_module():
    name = "better_m4_live_acceptance"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parents[1] / "scripts" / "m4_live_acceptance.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_m4_live_budget_arithmetic_is_frozen_before_network() -> None:
    from app.costs import PriceSnapshot, estimate_cost
    from app.model_gateway import UsageBuckets
    live = _live_module()

    price = PriceSnapshot("m4-price", 440_000, 14_000, 0, 1_320_000, 0)
    attempt = estimate_cost(UsageBuckets(32_768, 0, 0, 8_192, 0), price)
    assert attempt.microusd == live.WORST_ATTEMPT_MICROUSD == 25_232
    assert attempt.microusd * live.MAX_MODEL_ATTEMPTS == live.MAX_COST_MICROUSD == 756_960
    with pytest.raises(AssertionError):
        assert attempt.microusd * 31 <= live.MAX_COST_MICROUSD


def test_m4_frozen_suite_has_three_distinct_official_scopes() -> None:
    live = _live_module()

    assert len(live.CASES) == live.MAX_ROUNDS == 3
    assert len({case["id"] for case in live.CASES}) == 3
    for case in live.CASES:
        assert len(case["urls"]) == 2
        assert all(url.startswith("https://") for url in case["urls"])
