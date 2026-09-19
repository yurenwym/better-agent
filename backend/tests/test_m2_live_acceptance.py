import importlib.util
from pathlib import Path
import sys

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts" / "m2_live_acceptance.py"
    spec = importlib.util.spec_from_file_location("m2_live_acceptance_script", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_m2_preflight_is_bounded_and_does_not_start_network(monkeypatch):
    module = _module()
    monkeypatch.delenv("M2_LIVE_APPROVED", raising=False)
    result = module.preflight()
    assert result["status"] == "NOT_AUTHORISED"
    assert result["network_started"] is False
    assert result["limits"] == {
        "rounds": 3,
        "attempts_per_round": 10,
        "total_attempts": 30,
        "max_seconds": 1200,
        "worst_cost_microusd": 756_960,
        "fallback_models": 0,
    }


@pytest.mark.asyncio
async def test_batch_budget_blocks_before_attempt_31():
    module = _module()
    budget = module.BatchBudget()
    budget.attempts = [{"round": index // 10 + 1} for index in range(30)]
    with pytest.raises(module.AcceptanceFailure, match="batch attempt hard limit"):
        await budget.execute(4, None, None)


def test_preflight_declares_current_database_and_scoped_cleanup(monkeypatch):
    module = _module()
    monkeypatch.delenv("M2_LIVE_APPROVED", raising=False)
    value = module.preflight()
    assert value["database"] == "better_agent"
    assert value["storage"] == "current_database_with_batch_scoped_cleanup"
