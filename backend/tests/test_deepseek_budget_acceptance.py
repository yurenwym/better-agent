import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "budget_acceptance", Path(__file__).parents[1] / "scripts" / "deepseek_budget_acceptance.py",
)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def test_usage_pairs_attempts_without_mixing_classification_or_retries():
    attempts = [
        {"attempt_id": "classify", "purpose": "classify_existing_plan_save",
         "uncached_input_tokens": 166, "cache_read_tokens": 32000, "cache_write_tokens": 0},
        {"attempt_id": "failed", "purpose": "route_and_respond",
         "uncached_input_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None},
        {"attempt_id": "main", "purpose": "route_and_respond",
         "uncached_input_tokens": 50915, "cache_read_tokens": 1664, "cache_write_tokens": 0},
    ]
    estimates = {"main": {"estimated_input_tokens": 48564}}
    result = acceptance._pair_attempt_usage(attempts, estimates)
    assert result[0]["actual_input_tokens"] == 32166
    assert result[0]["relative_error_pct"] is None
    assert result[1]["actual_input_tokens"] is None
    assert result[2]["actual_input_tokens"] == 52579
    assert result[2]["relative_error_pct"] == -7.64


def test_successful_plain_chat_does_not_pass_ask_continuation_acceptance():
    rows = [{
        "index": index, "final_status": "COMPLETED", "timed_out": False,
        "assistant_answer_length": 50, "interactions": [], "diagnostics": [],
        "provider_attempts": [{"purpose": "route_and_respond", "status": "SUCCEEDED",
                               "actual_input_tokens": 100, "request_estimate": {"estimated_input_tokens": 90}}],
    } for index in range(1, 17)]
    assert acceptance._acceptance_errors(rows, True) == [
        "no completed ask form continuation with event evidence",
    ]
    rows[1].update(chain_turn_ids=["parent", "continuation"],
                   interactions=[{"action": "answer_ask"}],
                   diagnostics=[{"type": "ask.requested"}, {"type": "ask.answered"}])
    assert acceptance._acceptance_errors(rows, True) == []
    rows[3]["final_status"] = "FAILED"
    assert "round 4 did not deliver a completed answer" in acceptance._acceptance_errors(rows, True)
