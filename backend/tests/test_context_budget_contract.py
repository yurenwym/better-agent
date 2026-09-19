"""R1 acceptance: one budget entry point, tier gate, and A-tier request counting.

These tests pin the M1-02 / M1-03 contract:

* ``effective_input_budget`` is the only place that computes ``S`` and ``H``.
* A tier-A profile without an evidenced ``admitted_context_limit`` is refused,
  never silently degraded to ``min(soft, context_window)``.
* An unverified repository ``context_window`` default may not cap an evidenced A.
* Wrapper overhead is charged once, through the declared protocol budget, so a
  tier-A budget keeps a zero safety margin instead of stacking a fixed guard.
* ``count_request_units`` counts the complete request (``U_A``) and refuses to
  invent protocol coefficients the adapter never recorded.
"""

import json
import sqlite3

import pytest

from app.model_gateway import ModelProfile
from app.token_budget import (
    LEGACY_TIER,
    VALIDATION_TIER_A,
    ContextOverflow,
    ProtocolBudget,
    assert_request_fits,
    count_request_units,
    effective_input_budget,
    request_budget,
    strip_packing_hints,
)


def _profile(**overrides) -> ModelProfile:
    values = {
        "base_url": "https://example.test/v1",
        "model": "demo",
        "api_key_env": "TEST_KEY",
        "context_window": 32768,
        "max_output_tokens": 8192,
    }
    values.update(overrides)
    return ModelProfile(**values)


def _tier_a_profile(**overrides) -> ModelProfile:
    values = {
        "validation_tier": VALIDATION_TIER_A,
        "admitted_context_limit": 32768,
        "counter_evidence_version": "openai-compatible-wrapper-v1",
    }
    values.update(overrides)
    return _profile(**values)


# --------------------------------------------------------------------------- #
# M1-02 — the single budget entry point
# --------------------------------------------------------------------------- #


def test_legacy_profile_keeps_the_historical_proportional_margin() -> None:
    budget = effective_input_budget(_profile())

    assert budget.validation_tier == LEGACY_TIER
    assert budget.safety_margin == min(2048, (32768 - 8192) // 20) == 1228
    assert budget.input_limit == 32768 - 8192 - 1228 == 23348
    assert budget.evidence_version is None


def test_legacy_request_budget_wrapper_is_unchanged() -> None:
    legacy = request_budget(_profile())

    assert legacy.context_window == 32768
    assert legacy.reserved_output == 8192
    assert legacy.safety_margin == 1228
    assert legacy.input_limit == 23348


def test_tier_a_requires_an_evidenced_admitted_limit() -> None:
    with pytest.raises(ContextOverflow, match="admitted_context_limit"):
        effective_input_budget(_tier_a_profile(admitted_context_limit=None))


def test_tier_a_requires_counter_evidence_version() -> None:
    with pytest.raises(ContextOverflow, match="counter_evidence_version"):
        effective_input_budget(_tier_a_profile(counter_evidence_version="   "))


def test_tier_b_is_refused_rather_than_faked() -> None:
    with pytest.raises(ContextOverflow, match="not implemented"):
        effective_input_budget(_profile(validation_tier="B"))


def test_unknown_tier_is_refused() -> None:
    with pytest.raises(ContextOverflow, match="unknown validation_tier"):
        effective_input_budget(_profile(validation_tier="C"))


def test_tier_a_keeps_a_zero_safety_margin() -> None:
    budget = effective_input_budget(_tier_a_profile())

    assert budget.safety_margin == 0
    assert budget.total_limit == 32768
    assert budget.input_limit == 32768 - 8192 == 24576


def test_tier_a_takes_the_minimum_of_soft_admitted_and_verified_window() -> None:
    soft_wins = effective_input_budget(_tier_a_profile(soft_context_limit=16384))
    assert soft_wins.total_limit == 16384
    assert soft_wins.input_limit == 16384 - 8192 == 8192

    verified_window_wins = effective_input_budget(
        _tier_a_profile(context_window=24576, context_window_verified=True)
    )
    assert verified_window_wins.total_limit == 24576
    assert verified_window_wins.input_limit == 24576 - 8192 == 16384


def test_unverified_context_window_never_caps_an_evidenced_admitted_limit() -> None:
    """A repository default is not capacity evidence."""

    unverified = effective_input_budget(
        _tier_a_profile(context_window=12288, context_window_verified=False)
    )
    assert unverified.context_window is None
    assert unverified.total_limit == 32768
    assert unverified.input_limit == 32768 - 8192 == 24576

    verified = effective_input_budget(
        _tier_a_profile(context_window=12288, context_window_verified=True)
    )
    assert verified.context_window == 12288
    assert verified.total_limit == 12288
    assert verified.input_limit == 12288 - 8192 == 4096

    # A verified window that cannot even hold the output reserve is not a budget.
    with pytest.raises(ContextOverflow, match="empty"):
        effective_input_budget(
            _tier_a_profile(context_window=8192, context_window_verified=True)
        )


def test_tier_a_refuses_an_empty_effective_budget() -> None:
    with pytest.raises(ContextOverflow, match="output reserve"):
        effective_input_budget(_tier_a_profile(admitted_context_limit=4096))


def test_budget_public_view_exposes_the_declared_contract() -> None:
    view = effective_input_budget(_tier_a_profile()).public_view()

    assert view["validation_tier"] == VALIDATION_TIER_A
    assert view["admitted_context_limit"] == 32768
    assert view["input_limit"] == 24576
    assert view["evidence_version"] == "openai-compatible-wrapper-v1"
    assert view["counter_version"] == "utf8-upper-bound-v1"


# --------------------------------------------------------------------------- #
# M1-03 — complete request counting
# --------------------------------------------------------------------------- #


def test_protocol_budget_rejects_invalid_coefficients() -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        ProtocolBudget(b0=-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        ProtocolBudget(per_message=True)  # bool is not an integer coefficient
    with pytest.raises(ValueError, match="non-negative integer"):
        ProtocolBudget(per_tool_call=1.5)


def test_protocol_budget_declared_requires_recorded_evidence() -> None:
    assert ProtocolBudget(b0=4).declared is False
    assert ProtocolBudget(b0=4, evidence="   ").declared is False
    assert ProtocolBudget(b0=4, evidence="adapter wrapper probe 2026-09").declared is True


def test_count_request_units_refuses_an_undeclared_protocol_budget() -> None:
    with pytest.raises(ContextOverflow, match="declared protocol budget"):
        count_request_units([{"role": "user", "content": "hi"}], None, _tier_a_profile())

    with pytest.raises(ContextOverflow, match="declared protocol budget"):
        count_request_units(
            [{"role": "user", "content": "hi"}],
            None,
            _tier_a_profile(protocol_budget=ProtocolBudget(b0=12)),  # no evidence
        )


def test_count_request_units_charges_wrapper_overhead_once() -> None:
    profile = _tier_a_profile(
        protocol_budget=ProtocolBudget(
            b0=10, per_message=2, per_tool_definition=5, per_tool_call=7, per_tool_result=3,
            evidence="adapter wrapper probe 2026-09",
        )
    )
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "name": "t", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    tools = [{"type": "function", "function": {"name": "t"}}]

    counted = count_request_units(messages, tools, profile)

    assert counted.mode == "A"
    assert counted.message_count == 4
    assert counted.tool_definition_count == 1
    assert counted.tool_call_count == 1
    assert counted.tool_result_count == 1
    assert counted.protocol_bound == 10 + 2 * 4 + 5 * 1 + 7 * 1 + 3 * 1 == 33
    assert counted.byte_bound > 0
    assert counted.total == counted.byte_bound + counted.protocol_bound
    assert counted.evidence_version == "openai-compatible-wrapper-v1"


def test_count_request_units_ignores_local_packing_hints() -> None:
    profile = _tier_a_profile(protocol_budget=ProtocolBudget(b0=0, evidence="probe"))

    plain = count_request_units([{"role": "user", "content": "hi"}], None, profile)
    hinted = count_request_units(
        [{"role": "user", "content": "hi", "_context_priority": 70, "_context_group": "memory"}],
        None,
        profile,
    )

    assert hinted.byte_bound == plain.byte_bound
    assert hinted.total == plain.total


def test_strip_packing_hints_removes_only_local_keys() -> None:
    stripped = strip_packing_hints(
        [{"role": "user", "content": "hi", "_context_required": False, "_context_priority": 20}]
    )

    assert stripped == [{"role": "user", "content": "hi"}]


def test_assert_request_fits_uses_the_effective_budget() -> None:
    legacy = _profile()
    assert assert_request_fits([{"role": "user", "content": "hi"}], None, legacy) == len(
        json.dumps(
            {"messages": [{"role": "user", "content": "hi"}], "tools": []},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    )

    huge = _profile(context_window=8192, max_output_tokens=4096)
    with pytest.raises(ContextOverflow, match="conservative budget units"):
        assert_request_fits([{"role": "user", "content": "x" * 9000}], None, huge)


def test_final_gate_charges_protocol_overhead_only_for_tier_a() -> None:
    """The last gate must not let a request through on bytes alone at tier A."""

    profile = _tier_a_profile(protocol_budget=ProtocolBudget(b0=8192, evidence="probe"))
    messages = [{"role": "user", "content": "x" * 20000}]

    byte_only = count_request_units(messages, None, profile).byte_bound
    assert byte_only <= 24576  # fits on bytes alone

    with pytest.raises(ContextOverflow, match="protocol_bound=8192"):
        assert_request_fits(messages, None, profile)

    # Legacy profiles keep the byte-only historical behaviour.
    assert assert_request_fits(messages, None, _profile()) == byte_only


def test_final_gate_strips_packing_hints_before_counting() -> None:
    legacy = _profile()
    plain = assert_request_fits([{"role": "user", "content": "hi"}], None, legacy)
    hinted = assert_request_fits(
        [{"role": "user", "content": "hi", "_context_priority": 70, "_context_group": "memory"}],
        None,
        legacy,
    )

    assert hinted == plain


# --------------------------------------------------------------------------- #
# Persistence — migration 36 and the admin read-back
# --------------------------------------------------------------------------- #


def test_migration_36_adds_the_budget_contract_columns_and_refreezes_versions(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(model_profile_versions)")}

    assert 36 in versions
    assert versions[-1] >= 36
    assert {
        "admitted_context_limit",
        "soft_context_limit",
        "context_window_verified",
        "validation_tier",
        "counter_id",
        "counter_version",
        "counter_evidence_version",
        "capacity_evidence",
        "protocol_budget_json",
    } <= columns

    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO model_profiles(id,owner_id,name,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("profile-1", "local-user", "主要模型", "ACTIVE", "now", "now"),
        )
        connection.execute(
            "INSERT INTO model_profile_versions("
            "id,profile_id,version,provider_protocol,provider_name,base_url,model_name,credential_env_ref,"
            "capabilities_json,context_window,max_output_tokens,timeout_seconds,max_attempts,config_digest,created_at,"
            "admitted_context_limit,soft_context_limit,context_window_verified,validation_tier,counter_id,"
            "counter_version,counter_evidence_version,capacity_evidence,protocol_budget_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "profile-version-1", "profile-1", 1, "openai_compatible", "test", "https://example.test/v1",
                "demo", "TEST_KEY", json.dumps({"text": True}), 32768, 8192, 30.0, 2, "digest-1", "now",
                32768, 32768, 1, "A", "utf8-upper-bound", "utf8-upper-bound-v1",
                "openai-compatible-wrapper-v1", "vendor doc 2026-09",
                json.dumps({"b0": 12, "evidence": "probe"}),
            ),
        )

    # The frozen trigger must cover the new contract columns too.
    for column, value in (
        ("counter_evidence_version", "'tampered'"),
        ("admitted_context_limit", "1"),
        ("validation_tier", "'B'"),
        ("protocol_budget_json", "'{}'"),
    ):
        with db.connection() as connection, pytest.raises(sqlite3.IntegrityError, match="frozen"):
            connection.execute(
                f"UPDATE model_profile_versions SET {column}={value} WHERE id='profile-version-1'"
            )


def test_model_admin_round_trips_the_budget_contract(tmp_path) -> None:
    from app.db import Database
    from app.model_admin import ModelAdminError, ModelAdminService

    db = Database(tmp_path / "agent.db")
    service = ModelAdminService(db)
    base_payload = {
        "provider_protocol": "openai_compatible",
        "provider_name": "test",
        "base_url": "https://example.test/v1",
        "model_name": "demo",
        "credential_env_ref": "TEST_KEY",
        "capabilities": {"text": True, "streaming": True},
        "context_window": 32768,
        "max_output_tokens": 8192,
        "max_attempts": 2,
    }

    profile = service.create_profile({**base_payload, "name": "主要模型"})
    version_id = profile["versions"][0]["id"]
    legacy = service.version(version_id)
    assert legacy["validation_tier"] is None
    assert legacy["protocol_budget"] == {}

    with pytest.raises(ModelAdminError, match="admitted_context_limit"):
        service.add_version(profile["id"], {**base_payload, "validation_tier": "A"})

    with pytest.raises(ModelAdminError, match="counter_evidence_version"):
        service.add_version(
            profile["id"],
            {**base_payload, "validation_tier": "A", "admitted_context_limit": 32768},
        )

    with pytest.raises(ModelAdminError, match="protocol_budget"):
        service.add_version(
            profile["id"],
            {
                **base_payload,
                "validation_tier": "A",
                "admitted_context_limit": 32768,
                "counter_evidence_version": "openai-compatible-wrapper-v1",
            },
        )

    with pytest.raises(ModelAdminError, match="protocol_budget requires recorded evidence"):
        service.add_version(
            profile["id"],
            {
                **base_payload,
                "validation_tier": "A",
                "admitted_context_limit": 32768,
                "counter_evidence_version": "openai-compatible-wrapper-v1",
                "protocol_budget": {"b0": 12},
            },
        )

    with pytest.raises(ModelAdminError, match="disagree"):
        service.add_version(
            profile["id"],
            {
                **base_payload,
                "validation_tier": "A",
                "admitted_context_limit": 32768,
                "soft_context_limit": 4096,
                "counter_evidence_version": "openai-compatible-wrapper-v1",
                "protocol_budget": {"b0": 12, "evidence": "adapter wrapper probe"},
            },
        )

    version = service.add_version(
        profile["id"],
        {
            **base_payload,
            "validation_tier": "A",
            "admitted_context_limit": 32768,
            "soft_context_limit": 32768,
            "context_window_verified": True,
            "counter_evidence_version": "openai-compatible-wrapper-v1",
            "capacity_evidence": "vendor doc 2026-09",
            "protocol_budget": {
                "b0": 12, "per_message": 2, "evidence": "adapter wrapper probe 2026-09",
            },
        },
    )

    assert version["validation_tier"] == "A"
    assert version["admitted_context_limit"] == 32768
    assert version["soft_context_limit"] == 32768
    assert version["context_window_verified"] is True
    assert version["counter_evidence_version"] == "openai-compatible-wrapper-v1"
    assert version["capacity_evidence"] == "vendor doc 2026-09"
    assert version["protocol_budget"]["b0"] == 12
    assert version["protocol_budget"]["per_message"] == 2
    assert version["protocol_budget"]["declared"] is True
    # The contract participates in the digest, so a changed budget is a new version.
    assert version["config_digest"] != legacy["config_digest"]
