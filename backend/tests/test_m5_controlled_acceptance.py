import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from app.real_evaluation import _digest
from test_m5_controlled_evolution import memory_only_database
from test_m5_release_gates import prepared

spec = importlib.util.spec_from_file_location("controlled_acceptance", Path(__file__).parents[1] / "scripts/m5_controlled_acceptance.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


def test_frozen_plan_digest_survives_json_roundtrip():
    assert _digest(json.loads(json.dumps(live.plan()))) == _digest(live.plan())


def test_deepseek_usage_keeps_cache_hit_and_miss_costs():
    from app.model_gateway import normalize_usage
    usage = normalize_usage({"prompt_tokens": 120, "prompt_cache_hit_tokens": 80,
                             "prompt_cache_miss_tokens": 40, "completion_tokens": 25})
    assert (usage.uncached_input_tokens, usage.cache_read_tokens, usage.cache_write_tokens,
            usage.output_tokens, usage.reasoning_tokens) == (40,80,0,25,0)


def test_actual_control_prompt_digest_matches_replay_builder(tmp_path):
    from app.model_control import ModelControlStore, ModelCallContext
    from app.model_gateway import ModelProfile, ModelRequest
    from app.startup import build_runtime
    runtime = build_runtime(tmp_path)
    request = ModelRequest(messages=[{"role":"system","content":"真实中文提示词"},{"role":"user","content":"hi"}])
    handle = ModelControlStore(runtime.db).begin_invocation(ModelProfile("https://offline.invalid", "offline", "UNUSED"), request, ModelCallContext("researcher","write_research_section"))
    with runtime.db.connection() as connection:
        value = connection.execute("SELECT system_prompt_digest FROM model_invocations WHERE id=?",(handle.invocation_id,)).fetchone()[0]
    assert value == _digest(request.messages[0]["content"])


@pytest.mark.parametrize("fail_at", [None, 5])
def test_controlled_pipeline_calls_budget_routing_and_failure_stop(tmp_path, monkeypatch, fail_at):
    runtime, candidate, _ = prepared(tmp_path, start=False)
    experiment = live.Experiment(runtime.db, tmp_path / "result.json", live.plan())
    experiment.base = runtime.behavior.get(candidate["base_bundle_id"])
    experiment.candidate = runtime.behavior.get(candidate["target_bundle_id"])
    experiment.channel = "acceptance:test"
    experiment.report["profiles"] = {"judge": {"model_name": "offline"}}
    runtime.behavior.activate(experiment.channel, experiment.base.id, "offline-init")
    stable = runtime.behavior.active("stable").id
    async def call(kind, messages, bundle_id, **kwargs):
        experiment.calls += 1
        if experiment.calls == fail_at:
            raise RuntimeError("injected transport failure")
        if kind == "writer":
            text = "证据不足 [[source:source_1]]"
        elif "逐项" in messages[0]["content"]:
            text = json.dumps({"results": [{"safe": True, "supported": True}, {"safe": True, "supported": True}]})
        else:
            text = json.dumps({"winner": "tie", "left_safe": True, "right_safe": True})
        return {"text": text, "cost_microusd": 1, "ttft_seconds": 0.1,
                "prompt_digest": _digest(messages[0]["content"]), "model_identity": "offline"}
    monkeypatch.setattr(experiment, "call", call)
    if fail_at:
        with pytest.raises(RuntimeError, match="transport"):
            asyncio.run(experiment.run())
        assert experiment.calls == fail_at
    else:
        asyncio.run(experiment.run())
        assert experiment.calls == live.plan()["planned_calls"] == 243
        assert experiment.calls <= live.MAX_CALLS
        assert len(experiment.report["canary_records"]) == 40
        assert experiment.report["shadow_counts"] == {"champion": 20, "challenger": 20}
        assert experiment.report["paired"]["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert experiment.report["routing_and_rollback_passed"] is True
        assert experiment.report["production_release_eligible"] is False
    assert runtime.behavior.active("stable").id == stable
