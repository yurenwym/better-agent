"""The selector's ruler and the wire gate's ruler must be the same ruler.

``pack_messages_newest`` counts the canonical ``{"messages", "tools"}`` payload.
Every adapter then wraps that payload in an envelope. Before this test existed,
the selector packed against ``H`` - the wire gate's budget - so a request that
filled the canonical budget arrived at the gate ~60 units over and was refused,
even though nothing in it was too large. A live DeepSeek call found it:

    provider payload requires 23411 conservative budget units but the input
    budget is 23348 [keys=['max_tokens','messages','model','stream',
    'stream_options','temperature','thinking','tools']]

These tests pin the fix and measure the rewriting adapters instead of assuming
they behave like the envelope-preserving one.
"""

from __future__ import annotations

import json

import pytest

from app.model_gateway import (
    ModelGateway,
    ModelProfile,
    ModelRequest,
    provider_payload,
)
from app.token_budget import (
    _ENVELOPE_SHAPES,
    ContextOverflow,
    DEFAULT_TOKEN_COUNTER,
    assert_provider_payload_fits,
    effective_input_budget,
    envelope_overhead,
    hot_window,
    packing_limit,
    pack_messages_newest,
    per_message_overhead,
    per_tool_overhead,
    wire_units,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "查询",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
]


def _profile(**overrides) -> ModelProfile:
    values = {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "api_key_env": "TEST_KEY",
        "provider_protocol": "openai_compatible",
        "provider_name": "deepseek",
        "context_window": 32_768,
        "max_output_tokens": 8_192,
        "declared_capabilities": frozenset({"text", "streaming"}),
        "timeout_seconds": 5,
    }
    values.update(overrides)
    return ModelProfile(**values)


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "帮我安排一次杭州出行。"},
        {"role": "assistant", "content": "好的，我先确认几个参数。"},
        {"role": "user", "content": "预算 8000 元，出发日期 3 月 12 日。"},
    ]


def _wire_count(profile: ModelProfile, messages: list[dict], tools=None) -> int:
    request = ModelRequest(
        messages=messages, tools=tools or [], temperature=0, max_tokens=800,
        role="conversation", purpose="test", thinking=False,
    )
    return wire_units(provider_payload(profile, request))


# --------------------------------------------------------------------------- #
# The envelope is measured, and it is an upper bound for OpenAI
# --------------------------------------------------------------------------- #


def test_envelope_is_positive_for_a_real_adapter():
    assert envelope_overhead(_profile()) > 0


def test_envelope_reservation_equals_the_widest_measured_shape():
    """The reservation is the widest declared shape's overhead, not an average.

    ``envelope_overhead`` takes the maximum over the shapes it measures, so the
    reservation is at least as large as what any one shape needs. This pins that
    it is exactly the widest - no accidental extra slack, and no shape left
    uncovered.
    """
    profile = _profile()
    request = ModelRequest(messages=[], tools=[], **_ENVELOPE_SHAPES[-1])
    canonical = DEFAULT_TOKEN_COUNTER.count_payload([], [])
    assert envelope_overhead(profile) == wire_units(provider_payload(profile, request)) - canonical


@pytest.mark.parametrize("shape", _ENVELOPE_SHAPES)
def test_every_declared_shape_is_covered_by_the_reservation(shape):
    profile = _profile()
    request = ModelRequest(messages=[], tools=[], **shape)
    canonical = DEFAULT_TOKEN_COUNTER.count_payload([], [])
    assert wire_units(provider_payload(profile, request)) - canonical <= envelope_overhead(profile)


@pytest.mark.parametrize(
    "thinking, response_format, temperature, max_tokens",
    [
        (True, {"type": "json_object"}, 0.7, 4_096),
        (None, None, None, None),
        (False, None, 0, 800),
        (False, None, 0, 8_192),
    ],
)
def test_envelope_is_an_upper_bound_for_every_request_shape(
    thinking, response_format, temperature, max_tokens,
):
    """The reservation must never under-count, for any optional-field mix."""
    profile = _profile()
    request = ModelRequest(
        messages=_messages(), tools=TOOLS, temperature=temperature,
        max_tokens=max_tokens, thinking=thinking, response_format=response_format,
    )
    canonical = DEFAULT_TOKEN_COUNTER.count_payload(request.messages, request.tools)
    reserved = envelope_overhead(
        profile, message_count=len(request.messages), tool_count=len(request.tools),
    )
    assert wire_units(provider_payload(profile, request)) <= canonical + reserved


def test_non_deepseek_openai_compatible_has_a_smaller_envelope():
    """The envelope is measured per profile, not hardcoded for DeepSeek."""
    deepseek = envelope_overhead(_profile())
    other = envelope_overhead(_profile(provider_name="openrouter", base_url="https://openrouter.ai/api"))
    assert 0 < other < deepseek


@pytest.mark.parametrize(
    "protocol, expected_per_message",
    [("openai_compatible", 0), ("anthropic", 0), ("gemini", 9)],
)
def test_per_message_overhead_is_measured_per_adapter(protocol, expected_per_message):
    """Only a rewriting adapter has a per-message cost; measure, don't assume.

    Gemini wraps every message in ``parts``, so a constant reservation drifts:
    before this was measured the gap reached 3604 units on a 400-message
    conversation. The envelope-preserving adapter and Anthropic add nothing per
    message.
    """
    profile = _profile(provider_protocol=protocol, provider_name=protocol, base_url="https://example.test")
    assert per_message_overhead(profile) == expected_per_message


@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic", "gemini"])
def test_reservation_covers_a_long_conversation(protocol):
    """The reservation must not drift as the conversation grows."""
    profile = _profile(provider_protocol=protocol, provider_name=protocol, base_url="https://api.deepseek.com")
    for count in (1, 10, 100, 400):
        messages = [{"role": "user", "content": "预算 8000 元，出发日期 3 月 12 日。"}] * count
        request = ModelRequest(
            messages=messages, tools=TOOLS, temperature=0, max_tokens=800,
            role="conversation", purpose="test", thinking=False,
        )
        canonical = DEFAULT_TOKEN_COUNTER.count_payload(messages, TOOLS)
        reserved = envelope_overhead(
            profile, message_count=len(messages), tool_count=len(TOOLS),
        )
        assert wire_units(provider_payload(profile, request)) <= canonical + reserved


# --------------------------------------------------------------------------- #
# Packing against the packing limit produces a request the gate accepts
# --------------------------------------------------------------------------- #


def _fill_to(messages: list[dict], profile: ModelProfile, budget: int, tools=None) -> list[dict]:
    """Grow the newest user message until the selector's count reaches ``budget``.

    ``tools`` counts against the same budget the selector uses, so it has to be
    part of the measurement - otherwise the helper would pad the messages to the
    whole limit and then add tool definitions on top of it.
    """
    tools = tools or []
    padded = [dict(message) for message in messages]
    used = DEFAULT_TOKEN_COUNTER.count_payload(padded, tools)
    padded[-1]["content"] = padded[-1]["content"] + "x" * (budget - used)
    assert DEFAULT_TOKEN_COUNTER.count_payload(padded, tools) == budget
    return padded


def test_packing_limit_is_below_the_input_limit_by_the_reservation():
    profile = _profile()
    assert packing_limit(profile, message_count=7, tool_count=2) == (
        effective_input_budget(profile).input_limit
        - envelope_overhead(profile, message_count=7, tool_count=2)
    )


def test_hot_window_exposes_a_shape_aware_packing_limit():
    profile = _profile()
    window = hot_window(profile)
    assert window.packing_limit(7, 2) == packing_limit(profile, message_count=7, tool_count=2)
    view = window.public_view()
    assert view["envelope_units"] == window.envelope_units
    assert view["per_message_units"] == window.per_message_units


def test_hot_window_packing_limit_shrinks_as_the_request_grows():
    """A rewriting adapter's reservation has to grow with the message count."""
    window = hot_window(_profile(provider_protocol="gemini", provider_name="gemini",
                                 base_url="https://example.test"))
    assert window.packing_limit(400) < window.packing_limit(10) < window.packing_limit(0)


def test_request_packed_to_the_packing_limit_is_accepted_by_the_wire_gate():
    """The invariant the live failure broke."""
    profile = _profile()
    packed = _fill_to(_messages(), profile, packing_limit(profile, message_count=4))
    request = ModelRequest(
        messages=packed, tools=[], temperature=0, max_tokens=800,
        role="conversation", purpose="test", thinking=False,
    )
    assert_provider_payload_fits(provider_payload(profile, request), profile)


def test_request_packed_to_the_input_limit_is_refused():
    """Documents the defect: the same request against ``H`` is over budget.

    This is not a behaviour to preserve - it is the reason ``packing_limit``
    exists. If this test ever starts failing, the adapter envelope went away and
    the reservation is now unnecessary.
    """
    profile = _profile()
    packed = _fill_to(_messages(), profile, effective_input_budget(profile).input_limit)
    request = ModelRequest(
        messages=packed, tools=[], temperature=0, max_tokens=800,
        role="conversation", purpose="test", thinking=False,
    )
    with pytest.raises(ContextOverflow):
        assert_provider_payload_fits(provider_payload(profile, request), profile)


def test_selector_output_at_the_packing_limit_passes_the_gate():
    """End to end through the real selector, not a hand-built request."""
    profile = _profile()
    optional = [
        {"role": "system", "content": "技能上下文。" * 400,
         "_context_required": False, "_context_priority": 70, "_context_group": "skill-context"},
        {"role": "system", "content": "计划上下文。" * 300,
         "_context_required": False, "_context_priority": 50, "_context_group": "plan-context"},
    ]
    candidate = [*optional, *_messages()]
    packed = pack_messages_newest(
        candidate, budget=packing_limit(profile, message_count=len(candidate)),
    )
    request = ModelRequest(
        messages=packed, tools=[], temperature=0, max_tokens=800,
        role="conversation", purpose="test", thinking=False,
    )
    assert_provider_payload_fits(provider_payload(profile, request), profile)


# --------------------------------------------------------------------------- #
# Rewriting adapters: measured, not assumed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_rewriting_adapters_do_not_exceed_the_reserved_budget(protocol):
    """Anthropic/Gemini rebuild the messages, so the wire gate stays the authority.

    What must hold is the weaker, sufficient property: a request packed to the
    packing limit still fits the wire budget once the adapter has rewritten it.
    If this ever fails, the reservation needs a different shape term rather than
    a constant.
    """
    profile = _profile(provider_protocol=protocol, provider_name=protocol, base_url="https://example.test")
    candidate = _messages()
    packed = _fill_to(
        candidate, profile,
        packing_limit(profile, message_count=len(candidate), tool_count=len(TOOLS)),
        TOOLS,
    )
    request = ModelRequest(
        messages=packed, tools=TOOLS, temperature=0, max_tokens=800,
        role="conversation", purpose="test", thinking=False,
    )
    total = wire_units(provider_payload(profile, request))
    assert total <= effective_input_budget(profile).input_limit


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_rewriting_adapters_still_hoist_system_out_of_messages(protocol):
    """The refactor must not have changed what those adapters send."""
    profile = _profile(provider_protocol=protocol, provider_name=protocol, base_url="https://example.test")
    request = ModelRequest(messages=_messages(), tools=TOOLS, temperature=0, max_tokens=800)
    payload = provider_payload(profile, request)
    if protocol == "anthropic":
        assert "system" in payload
        assert all(message["role"] != "system" for message in payload["messages"])
        assert payload["tools"][0]["input_schema"] == TOOLS[0]["function"]["parameters"]
    else:
        assert "systemInstruction" in payload
        assert all("system" not in json.dumps(content) for content in payload["contents"])
        assert payload["tools"][0]["functionDeclarations"][0]["parameters"] == TOOLS[0]["function"]["parameters"]


def test_adapters_never_leak_packing_hints_to_the_provider():
    profile = _profile()
    hints = [{**message, "_context_priority": 60, "_context_required": False, "_context_group": "memory-context"}
             for message in _messages()]
    for protocol in ("openai_compatible", "anthropic", "gemini"):
        payload = provider_payload(
            _profile(provider_protocol=protocol, provider_name=protocol, base_url="https://example.test"),
            ModelRequest(messages=hints, tools=[], temperature=0, max_tokens=100),
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "_context_priority" not in serialized
        assert "_context_group" not in serialized
        assert "_context_required" not in serialized


def test_gateway_and_module_helpers_agree():
    profile = _profile()
    gateway = ModelGateway(profile)
    assert gateway.packing_limit(message_count=5, tool_count=1) == packing_limit(
        profile, message_count=5, tool_count=1,
    )
    assert gateway.input_limit() == effective_input_budget(profile).input_limit
