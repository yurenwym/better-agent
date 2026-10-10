import copy
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.evals import load_routing_suite, routing_holdout_record, run_routing_mock

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "evals/cases/agent-loop-routing-v1.json"
FREEZE = ROOT / "docs/acceptance/agent-loop-m1/holdout-freeze.json"


def test_mock_covers_every_case_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    suite = load_routing_suite(CASES, FREEZE)
    report = run_routing_mock(suite)
    assert len(report["cases"]) == 70
    assert report["by_partition"]["HOLDOUT"]["planned"] == 24
    assert [(c["case_id"], c["partition"], c["category"]) for c in report["cases"]] == [
        (c["case_id"], c["partition"], c["category"]) for c in suite["cases"]
    ]
    assert report["production_route_executed"] is False
    assert report["routing_quality"] == "INSUFFICIENT_EVIDENCE"
    assert report["summary"]["matched"] < 70
    assert all(c["first_token_ms"] is None and c["cost_usd"] is None for c in report["cases"])


def test_dev_changes_leave_holdout_frozen_and_holdout_changes_are_rejected(tmp_path):
    suite = load_routing_suite(CASES, FREEZE)
    before = routing_holdout_record(suite)
    next(c for c in suite["cases"] if c["partition"] == "DEV")["input"] = "另一个合成问题"
    assert routing_holdout_record(suite) == before
    changed = tmp_path / "cases.json"
    changed.write_text(json.dumps(suite), encoding="utf-8")
    load_routing_suite(changed, FREEZE)
    next(c for c in suite["cases"] if c["partition"] == "HOLDOUT")["input"] = "变更"
    changed.write_text(json.dumps(suite), encoding="utf-8")
    with pytest.raises(ValueError, match="freeze mismatch"):
        load_routing_suite(changed, FREEZE)


def test_invalid_cancelled_missing_and_forbidden_remain_in_denominator():
    suite = load_routing_suite(CASES, FREEZE)
    first, second, third = [c["case_id"] for c in suite["cases"][:3]]
    denied = next(c for c in suite["cases"] if c["category"] == "negation")["case_id"]
    report = run_routing_mock(suite, responses={
        first: {"status": "invalid"},
        second: {"status": "cancelled"},
        third: {"status": "valid", "observed": ["final"]},
        denied: {"status": "valid", "observed": ["final", "handoff:research"]},
    })
    assert report["summary"] == {
        "planned": 70, "matched": 1, "match_fraction": 1 / 70,
        "invalid": 67, "cancelled": 1, "forbidden_hits": 1,
    }
    assert len(report["invalid_case_ids"]) == 67
    assert report["cancelled_case_ids"] == [second]


def test_mock_does_not_copy_expected_labels():
    suite = load_routing_suite(CASES, FREEZE)
    changed = copy.deepcopy(suite)
    changed["cases"][0]["expected"] = ["ask"]
    before, after = run_routing_mock(suite), run_routing_mock(changed)
    assert before["cases"][0]["observed"] == after["cases"][0]["observed"] == ["final"]
    assert before["cases"][0]["matched"] and not after["cases"][0]["matched"]


def test_cli_writes_offline_json(tmp_path):
    output = tmp_path / "report.json"
    result = subprocess.run([
        sys.executable, str(ROOT / "backend/scripts/eval_agent_loop_routing.py"),
        "--mode", "legacy", "--provider", "mock", "--output", str(output),
    ], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["planned"] == 70
    assert report["provider"] == "mock"
