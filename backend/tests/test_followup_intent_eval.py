import importlib.util
import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

PATH = Path(__file__).resolve().parents[1] / "scripts/followup_intent_eval.py"
spec = importlib.util.spec_from_file_location("followup_intent_eval", PATH)
evaluation = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evaluation
spec.loader.exec_module(evaluation)


def test_gold_does_not_enter_model_prompt():
    case = {"id": "secret-id", "input": "做个攻略", "history": [],
            "expected": {"review": "SENTINEL_GOLD"}, "rationale": "SENTINEL_RATIONALE"}
    payload = json.dumps(evaluation.probe_messages(case), ensure_ascii=False)
    assert "SENTINEL" not in payload and "secret-id" not in payload


def test_errors_count_as_misses_and_false_daily_requests_are_visible():
    gold = {"tracking": "enable", "review": "daily", "document": "answer"}
    rows = [
        {"id": "1", "mode": "probe", "status": "error", "expected": gold},
        {"id": "2", "mode": "probe", "status": "ok", "expected": {**gold, "review": "none"}, "prediction": gold},
        {"id": "3", "mode": "probe", "status": "ok", "expected": gold, "prediction": gold},
    ]
    result = evaluation.score(rows)
    assert result["n"] == 3 and result["exact_correct"] == 1
    assert result["daily"] == {"tp": 1, "fp": 1, "fn": 1, "precision": .5, "recall": .5}


def test_dataset_has_unique_cases_and_covers_frequency_and_cancellation():
    cases = json.loads(evaluation.DATA.read_text(encoding="utf-8"))
    assert len(cases) >= 30 and len({c["id"] for c in cases}) == len(cases)
    assert {c["expected"]["review"] for c in cases} == {"none", "once", "daily", "weekly", "stop", "clarify"}
    for case in cases:
        evaluation.Intent.model_validate({**case["expected"], "reason": "gold"})


def test_invalid_labels_and_extra_fields_are_not_silently_scored():
    with pytest.raises(ValidationError):
        evaluation.Intent.model_validate({"tracking": "none", "review": "sometimes", "document": "answer", "reason": ""})


def test_nested_gateway_dataclasses_are_serialized(tmp_path):
    path = tmp_path / "result.json"
    evaluation.write_json(path, {"response": {"usage": evaluation.UsageBuckets(output_tokens=12)}})
    assert json.loads(path.read_text(encoding="utf-8"))["response"]["usage"]["output_tokens"] == 12


def test_recovery_uses_saved_response_without_network(tmp_path, monkeypatch):
    async def unexpected_network(*args, **kwargs):
        raise AssertionError("offline recovery must not call network")
    monkeypatch.setattr(evaluation.ModelGateway, "complete", unexpected_network)
    from app.model_gateway import ModelProfile
    request = evaluation.ModelRequest(messages=[{"role": "user", "content": "hello"}])
    response = evaluation.ModelResponse("ok", [], "stop", evaluation.UsageBuckets(output_tokens=1), evaluation.Timing(1, 2, 3), 1)
    evaluation.write_json(tmp_path / "case-call-01.json", {"request": evaluation.asdict(request), "status": "ok", "response": response})
    budget = {"calls": 0, "limit": 1}
    gateway = evaluation.RecordedGateway(ModelProfile("https://unused.invalid", "unused", "UNUSED_KEY"), tmp_path,
                                        "case", asyncio.Semaphore(1), budget, replay_only=True)
    actual = asyncio.run(gateway.complete(request))
    assert actual.message == "ok" and budget["calls"] == 0


def test_recovery_rejects_changed_request(tmp_path):
    from app.model_gateway import ModelProfile
    evaluation.write_json(tmp_path / "case-call-01.json", {"request": {"messages": []}, "status": "ok"})
    gateway = evaluation.RecordedGateway(ModelProfile("https://unused.invalid", "unused", "UNUSED_KEY"), tmp_path,
                                        "case", asyncio.Semaphore(1), {"calls": 0, "limit": 1}, replay_only=True)
    with pytest.raises(RuntimeError, match="recovery_request_mismatch"):
        asyncio.run(gateway.complete(evaluation.ModelRequest(messages=[{"role": "user", "content": "changed"}])))
