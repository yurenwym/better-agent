from __future__ import annotations

import json
import subprocess
import sys


def test_m4_preflight_is_no_network_and_waits_for_m3(tmp_path, monkeypatch):
    monkeypatch.delenv("M4_LIVE_APPROVED", raising=False)
    output = tmp_path / "m4.json"
    subprocess.run([sys.executable, "scripts/m4_live_acceptance.py", "--output", str(output)], cwd=".", check=True)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_AUTHORISED"
    assert report["network_started"] is False
    assert report["limits"]["model_attempts"] == 30
    assert report["limits"]["source_requests"] == 12
    assert report["limits"]["network_retries"] == 0
    assert report["limits"]["structured_output_retries"] == 0
    assert report["limits"]["worst_batch_microusd"] == 756_960
    assert report["storage"] == "current_better_agent_with_batch_scoped_cleanup"
    assert "M3 passes" in report["reason"]


def test_m5_preflight_freezes_canary_gate_and_is_no_network(tmp_path, monkeypatch):
    monkeypatch.delenv("M5_LIVE_APPROVED", raising=False)
    output = tmp_path / "m5.json"
    subprocess.run([sys.executable, "scripts/m5_live_acceptance.py", "--output", str(output)], cwd=".", check=True)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "NOT_AUTHORISED"
    assert report["network_started"] is False
    assert report["limits"]["model_attempts"] == 300
    assert report["limits"]["minimum_challenger_samples"] == 20
    assert "M4 passes" in report["reason"]
