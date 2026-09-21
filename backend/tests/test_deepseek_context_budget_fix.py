"""DeepSeek context-budget minimal fix acceptance.

Covers the three deliverables of the plan:

* the official DeepSeek default window survives register -> store -> reload,
* the estimator (ceil(bytes/4)) is the one ruler for packing and the wire gate,
* a controlled 32k profile proves the fix is not masked by the 1M default.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.db import Database
from app.model_admin import ModelAdminService
from app.model_control import RoutedModelGateway
from app.model_gateway import ModelProfile, ModelRequest, provider_payload
from app.token_budget import (
    DEFAULT_TOKEN_COUNTER,
    ContextOverflow,
    assert_provider_payload_fits,
    assert_request_fits,
    counter_for_profile,
    effective_input_budget,
    hot_window,
    packing_limit,
    wire_units,
)


def _official_profile(**overrides) -> ModelProfile:
    values = dict(
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        api_key_env="TEST_KEY",
        context_window=32_768,
        max_output_tokens=8_192,
        counter_id="deepseek-text-estimate",
        counter_version="deepseek-text-estimate-v1",
    )
    values.update(overrides)
    return ModelProfile(**values)


def _env_profile(monkeypatch) -> ModelProfile:
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENT_MODEL_ID", "deepseek-flash")
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "fake-test")
    from app.config import load_model_profile_from_env

    return load_model_profile_from_env()


def _reload(service: ModelAdminService, version_id: str) -> ModelProfile:
    with service.db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM model_profile_versions WHERE id=?", (version_id,),
        ).fetchone()
    return RoutedModelGateway._profile(row)


def test_official_default_window_and_counter_survive_registration_and_reload(tmp_path, monkeypatch):
    profile = _env_profile(monkeypatch)
    assert profile.context_window == 1_000_000
    assert profile.capacity_status == "official-default"
    assert profile.counter_id == "deepseek-text-estimate"

    service = ModelAdminService(Database(tmp_path / "agent.db"))
    bound = service.ensure_profile(profile)
    reloaded = _reload(service, bound.registered_profile_version_id)

    for item in (profile, reloaded):
        assert item.context_window == 1_000_000
        assert item.working_window_mode == "auto"
        assert item.capacity_status == "official-default"
        assert item.capacity_source == "catalog-official-default"
        assert item.counter_id == "deepseek-text-estimate"
        assert item.counter_version == "deepseek-text-estimate-v1"
        assert item.counter_mode == "estimate"
        assert item.model_max_output_limit == 393_216
    assert hot_window(reloaded).input_limit == hot_window(profile).input_limit


def test_changing_the_counter_creates_a_new_version(tmp_path, monkeypatch):
    profile = _env_profile(monkeypatch)
    service = ModelAdminService(Database(tmp_path / "agent.db"))
    first = service.ensure_profile(profile)

    changed = replace(
        profile,
        counter_id="utf8-upper-bound",
        counter_version="utf8-upper-bound-v1",
    )
    second = service.ensure_profile(changed)

    assert second.registered_profile_version_id != first.registered_profile_version_id
    assert service.version(first.registered_profile_version_id)["counter_id"] == "deepseek-text-estimate"
    assert service.version(second.registered_profile_version_id)["counter_id"] == "utf8-upper-bound"
    # The frozen old version is untouched; a new task selects the new one.
    assert service.version(first.registered_profile_version_id)["config_digest"] != \
        service.version(second.registered_profile_version_id)["config_digest"]


def test_routed_profile_resolution_failure_is_a_configuration_error(tmp_path):
    from types import SimpleNamespace

    from test_runtime import make_runtime

    from app.model_gateway import GatewayError

    class RouteModel:
        class _Gateway:
            def resolved_profile(self, context=None, **kwargs):
                raise RuntimeError("routing table is broken")

        gateway = _Gateway()

        async def route_and_respond(self, **_kwargs):
            raise AssertionError("not used")

    runtime = make_runtime(tmp_path, RouteModel())
    thread = runtime.conversation.create_thread("window")
    accepted = runtime.conversation.accept_turn(thread.id, "window-1", "hi", [])
    turn = runtime.conversation.turn(accepted.turn_id)

    with pytest.raises(GatewayError) as caught:
        runtime.turn_worker.primary._hot_window(turn, "local-user")

    assert caught.value.kind == "configuration"


def test_32k_profile_uses_tokens_at_the_final_gate_not_raw_bytes():
    profile = _official_profile()
    budget = effective_input_budget(profile)
    assert budget.input_limit == 23_348  # 32768 - 8192 - 1228 legacy margin

    # ~26.7k Chinese characters: ~80k UTF-8 bytes, but ~20k estimated tokens.
    content = "旅" * 26_666
    request = ModelRequest(messages=[{"role": "user", "content": content}], max_tokens=8_192)
    payload = provider_payload(profile, request)

    byte_units = wire_units(payload, counter=DEFAULT_TOKEN_COUNTER)
    estimated_units = assert_provider_payload_fits(payload, profile)

    assert byte_units > budget.input_limit, "the old byte ruler would have rejected this"
    assert estimated_units <= budget.input_limit
    assert assert_request_fits(request.messages, request.tools, profile) <= budget.input_limit


def test_estimated_tokens_over_budget_are_still_rejected():
    profile = _official_profile()
    # ~32k Chinese characters estimate to ~24k tokens, above the 23,348 budget.
    content = "旅" * 32_000
    request = ModelRequest(messages=[{"role": "user", "content": content}], max_tokens=8_192)
    payload = provider_payload(profile, request)

    with pytest.raises(ContextOverflow):
        assert_provider_payload_fits(payload, profile)
    with pytest.raises(ContextOverflow):
        assert_request_fits(request.messages, request.tools, profile)


def test_packing_and_hot_window_share_the_profile_counter():
    profile = _official_profile()
    selection = counter_for_profile(profile)
    assert selection.counter_id == "deepseek-text-estimate"
    assert selection.mode == "estimate"
    assert "may undercount" in selection.applicability

    window = hot_window(profile)
    assert window.counter is selection.counter
    assert window.counter_mode == "estimate"
    assert window.reserved_output == 8_192
    assert window.input_limit == effective_input_budget(profile).input_limit

    from app.token_budget import envelope_overhead

    expected = max(
        window.input_limit - envelope_overhead(profile, message_count=12, tool_count=11, counter=selection.counter),
        0,
    )
    assert packing_limit(profile, message_count=12, tool_count=11) == expected


def test_byte_limits_are_not_scaled_by_the_token_estimate():
    profile = _official_profile()
    window = hot_window(profile)
    assert window.tool_result_bytes == max(window.input_limit // 8, 256)
    # Recent-window and tool-result limits are byte limits and keep their units.
    assert window.recent_window_bytes > 0
    assert window.tool_result_bytes > 256


def test_tools_and_history_participate_in_the_estimate():
    profile = _official_profile()
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"goal_tool_{index}",
                "description": "业务工具描述" * 20,
                "parameters": {
                    "type": "object",
                    "properties": {"document_id": {"type": "string", "description": "计划文档标识" * 10}},
                    "additionalProperties": False,
                },
            },
        }
        for index in range(11)
    ]
    messages = [
        {"role": "user", "content": "帮我做一份桂林旅游攻略"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "ask_user", "arguments": json.dumps({"questions": []}, ensure_ascii=False)},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "我补充了以下信息：" + "详情" * 200},
        {"role": "user", "content": "出行时间：近期出发；同行人：情侣；预算与节奏：度假型"},
    ]
    without_tools = assert_request_fits(messages, [], profile)
    with_tools = assert_request_fits(messages, tools, profile)
    assert with_tools > without_tools
    assert with_tools - without_tools >= sum(len(json.dumps(tool, ensure_ascii=False)) // 4 for tool in tools) // 2
