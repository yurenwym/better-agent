"""T02/T03: capacity contract and endpoint/version catalog."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.model_capacity import (
    CAPACITY_STATUS_MANUAL,
    CAPACITY_STATUS_OFFICIAL_DEFAULT,
    CAPACITY_STATUS_UNVERIFIED,
    CAPACITY_STATUS_VERIFIED,
    CAPACITY_SOURCE_OFFICIAL_DEFAULT,
    CATALOG,
    DEEPSEEK_FLASH,
    DEEPSEEK_FLASH_ANTHROPIC,
    COUNTER_MODE_ESTIMATE,
    COUNTER_MODE_VERIFIED,
    CapacityContractError,
    CapacityEvidence,
    dumps_capacity_record,
    find_capacity_entry,
    loads_capacity_record,
    normalize_endpoint,
    resolve_working_window,
)


def _verified_entry(**overrides):
    values = {
        "provider": "synthetic",
        "base_url": "https://capacity.test/v1",
        "protocol": "openai_compatible",
        "model_id": "synthetic-large",
        "model_version": "Synthetic-Large-1",
        "aliases": ("synthetic-large-alias",),
        "context_limit": 1_048_576,
        "max_output_limit": 131_072,
        "counter_id": "synthetic-exact",
        "counter_version": "synthetic-exact-v1",
        "counter_verified": True,
        "context_verified": True,
        "verified_at": "2026-09-19T00:00:00Z",
        "evidence": CapacityEvidence(
            urls=("https://capacity.test/docs",),
            fetched_at="2026-09-19T00:00:00Z",
        ),
    }
    values.update(overrides)
    return replace(DEEPSEEK_FLASH, **values)


# ---------------------------------------------------------------------------
# Endpoint normalization and matching
# ---------------------------------------------------------------------------

def test_endpoint_normalization_preserves_paths_and_removes_only_noise():
    assert normalize_endpoint("HTTPS://API.DeepSeek.com/") == "https://api.deepseek.com"
    assert normalize_endpoint("https://api.deepseek.com:443") == "https://api.deepseek.com"
    assert normalize_endpoint("https://api.deepseek.com/anthropic/") == (
        "https://api.deepseek.com/anthropic"
    )
    # A tenant or proxy path is a different endpoint, not the official one.
    assert normalize_endpoint("https://proxy.test/tenant-a") != normalize_endpoint(
        "https://proxy.test/tenant-b"
    )


def test_official_endpoint_hits_the_official_entry_and_protocols_stay_separate():
    openai_entry = find_capacity_entry(
        "https://api.deepseek.com", "openai_compatible", "deepseek-flash", entries=CATALOG,
    )
    anthropic_entry = find_capacity_entry(
        "https://api.deepseek.com/anthropic", "anthropic", "deepseek-flash", entries=CATALOG,
    )
    assert openai_entry is DEEPSEEK_FLASH
    assert anthropic_entry is DEEPSEEK_FLASH_ANTHROPIC
    # The official host with the wrong protocol must not silently match.
    assert find_capacity_entry(
        "https://api.deepseek.com", "anthropic", "deepseek-flash", entries=CATALOG,
    ) is None


def test_alias_and_unknown_model_matching():
    assert find_capacity_entry(
        "https://api.deepseek.com", "openai_compatible", "deepseek-v4-flash", entries=CATALOG,
    ) is DEEPSEEK_FLASH
    assert find_capacity_entry(
        "https://api.deepseek.com", "openai_compatible", "some-other-model", entries=CATALOG,
    ) is None
    assert find_capacity_entry(
        "https://proxy.test/v1", "openai_compatible", "deepseek-flash", entries=CATALOG,
    ) is None


# ---------------------------------------------------------------------------
# Auto / manual resolution
# ---------------------------------------------------------------------------

def test_deepseek_auto_uses_the_official_default_window_without_claiming_verification():
    resolution = resolve_working_window(
        base_url="https://api.deepseek.com",
        protocol="openai_compatible",
        model_id="deepseek-flash",
        mode="auto",
        max_output_tokens=8_192,
        entries=CATALOG,
    )
    assert resolution.status == CAPACITY_STATUS_OFFICIAL_DEFAULT
    assert resolution.source == CAPACITY_SOURCE_OFFICIAL_DEFAULT
    assert resolution.effective_context_limit == 1_000_000
    assert resolution.model_context_limit is None  # exact integer stays unresolved
    assert resolution.default_context_limit == 1_000_000
    assert resolution.model_max_output_limit == 393_216
    assert "conservative integer default" in resolution.reason
    assert resolution.counter_mode == COUNTER_MODE_ESTIMATE
    assert resolution.counter_id == "deepseek-text-estimate"


def test_unknown_proxy_auto_is_unverified_and_does_not_inherit_official_capacity():
    resolution = resolve_working_window(
        base_url="https://proxy.test/v1",
        protocol="openai_compatible",
        model_id="deepseek-flash",
        mode="auto",
        entries=CATALOG,
    )
    assert resolution.status == CAPACITY_STATUS_UNVERIFIED
    assert resolution.source == "catalog-miss"
    assert resolution.effective_context_limit is None
    assert resolution.model_max_output_limit is None


def test_verified_catalog_auto_follows_the_model_instead_of_32k():
    entry = _verified_entry()
    resolution = resolve_working_window(
        base_url="https://capacity.test/v1",
        protocol="openai_compatible",
        model_id="synthetic-large",
        mode="auto",
        max_output_tokens=8_192,
        entries=(entry,),
    )
    assert resolution.status == CAPACITY_STATUS_VERIFIED
    assert resolution.effective_context_limit == 1_048_576
    assert resolution.effective_context_limit != 32_768
    assert resolution.counter_mode == COUNTER_MODE_VERIFIED
    assert resolution.counter_id == "synthetic-exact"


def test_smaller_endpoint_limit_wins_over_larger_model_capacity():
    entry = _verified_entry()
    resolution = resolve_working_window(
        base_url="https://capacity.test/v1",
        protocol="openai_compatible",
        model_id="synthetic-large",
        mode="auto",
        admitted_context_limit=262_144,
        entries=(entry,),
    )
    assert resolution.effective_context_limit == 262_144


def test_manual_window_can_be_smaller_and_is_not_clamped_silently():
    entry = _verified_entry()
    resolution = resolve_working_window(
        base_url="https://capacity.test/v1",
        protocol="openai_compatible",
        model_id="synthetic-large",
        mode="manual",
        explicit_context_window=32_768,
        max_output_tokens=8_192,
        entries=(entry,),
    )
    assert resolution.status == CAPACITY_STATUS_MANUAL
    assert resolution.effective_context_limit == 32_768
    assert resolution.model_context_limit == 1_048_576

    with pytest.raises(CapacityContractError):
        resolve_working_window(
            base_url="https://capacity.test/v1",
            protocol="openai_compatible",
            model_id="synthetic-large",
            mode="manual",
            explicit_context_window=2_000_000,
            entries=(entry,),
        )


def test_manual_window_must_not_exceed_the_endpoint_limit():
    with pytest.raises(CapacityContractError):
        resolve_working_window(
            base_url="https://api.deepseek.com",
            protocol="openai_compatible",
            model_id="deepseek-flash",
            mode="manual",
            explicit_context_window=500_000,
            admitted_context_limit=262_144,
            entries=CATALOG,
        )


def test_output_budget_is_independent_from_the_model_output_limit():
    entry = _verified_entry()
    resolution = resolve_working_window(
        base_url="https://capacity.test/v1",
        protocol="openai_compatible",
        model_id="synthetic-large",
        mode="auto",
        max_output_tokens=8_192,
        entries=(entry,),
    )
    # 8192 stays the current output reserve; the 131072 model limit never
    # replaces it automatically.
    assert resolution.model_max_output_limit == 131_072
    with pytest.raises(CapacityContractError):
        resolve_working_window(
            base_url="https://capacity.test/v1",
            protocol="openai_compatible",
            model_id="synthetic-large",
            mode="auto",
            max_output_tokens=200_000,
            entries=(entry,),
        )


def test_output_reserve_must_be_smaller_than_the_working_window():
    entry = _verified_entry(context_limit=4_096, max_output_limit=4_096)
    with pytest.raises(CapacityContractError):
        resolve_working_window(
            base_url="https://capacity.test/v1",
            protocol="openai_compatible",
            model_id="synthetic-large",
            mode="auto",
            max_output_tokens=4_096,
            entries=(entry,),
        )


def test_conflicting_manual_channels_are_rejected():
    with pytest.raises(CapacityContractError):
        resolve_working_window(
            base_url="https://api.deepseek.com",
            protocol="openai_compatible",
            model_id="deepseek-flash",
            mode="manual",
            explicit_context_window=32_768,
            soft_context_limit=16_384,
            entries=CATALOG,
        )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# T04: configuration entry points
# ---------------------------------------------------------------------------

def _set_generic(monkeypatch, base_url="https://api.deepseek.com", model="deepseek-flash"):
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", base_url)
    monkeypatch.setenv("AGENT_MODEL_ID", model)
    monkeypatch.setenv("AGENT_MODEL_API_KEY", "secret")


def test_env_loader_labels_official_deepseek_auto_as_official_default(monkeypatch):
    from app.config import load_model_profile_from_env

    _set_generic(monkeypatch)
    profile = load_model_profile_from_env()
    assert profile.context_window == 1_000_000
    assert profile.working_window_mode == "auto"
    assert profile.capacity_status == CAPACITY_STATUS_OFFICIAL_DEFAULT
    assert profile.capacity_source == CAPACITY_SOURCE_OFFICIAL_DEFAULT
    assert profile.model_max_output_limit == 393_216
    assert profile.counter_id == "deepseek-text-estimate"
    record = loads_capacity_record(profile.capacity_evidence)
    assert record is not None and record["status"] == CAPACITY_STATUS_OFFICIAL_DEFAULT


def test_explicit_generic_window_is_manual_and_kept(monkeypatch):
    from app.config import load_model_profile_from_env

    _set_generic(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW", "16384")
    profile = load_model_profile_from_env()
    assert profile.context_window == 16_384
    assert profile.working_window_mode == "manual"
    assert profile.capacity_status == CAPACITY_STATUS_MANUAL


def test_explicit_32768_stays_32k(monkeypatch):
    from app.config import load_model_profile_from_env

    _set_generic(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW", "32768")
    assert load_model_profile_from_env().context_window == 32_768


def test_generic_and_vendor_entry_points_agree(monkeypatch):
    from app.config import load_model_profile_from_environment, load_model_profile_from_env

    _set_generic(monkeypatch)
    generic = load_model_profile_from_env()
    for name in ("AGENT_MODEL_BASE_URL", "AGENT_MODEL_ID", "AGENT_MODEL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    vendor = load_model_profile_from_environment()
    assert vendor is not None
    assert vendor.context_window == generic.context_window == 1_000_000
    assert vendor.capacity_status == generic.capacity_status == CAPACITY_STATUS_OFFICIAL_DEFAULT
    assert vendor.model_max_output_limit == generic.model_max_output_limit == 393_216


def test_vendor_explicit_window_is_manual(monkeypatch):
    from app.config import load_model_profile_from_environment

    monkeypatch.setenv("AGENT_MODEL_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("DEEPSEEK_CONTEXT_WINDOW", "65536")
    profile = load_model_profile_from_environment()
    assert profile.context_window == 65_536
    assert profile.working_window_mode == "manual"


def test_llm_file_window_is_manual(monkeypatch, tmp_path):
    from app.config import load_llm_ap

    path = tmp_path / "LLM_AP"
    path.write_text(
        "LLM_API_KEY=secret\nLLM_BASE_URL=https://api.deepseek.com\n"
        "LLM_MODEL_ID=deepseek-flash\nLLM_CONTEXT_WINDOW=65536\n",
        encoding="utf-8",
    )
    profile = load_llm_ap(path)
    assert profile.context_window == 65_536
    assert profile.working_window_mode == "manual"
    assert profile.capacity_source == "llm-file"


def test_manual_window_larger_than_a_known_endpoint_limit_is_rejected(monkeypatch):
    from app.config import load_model_profile_from_env
    from app.model_capacity import CapacityContractError

    _set_generic(monkeypatch)
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW", "500000")
    monkeypatch.setenv("AGENT_MODEL_ADMITTED_CONTEXT_LIMIT", "262144")
    with pytest.raises(CapacityContractError):
        load_model_profile_from_env()


# ---------------------------------------------------------------------------
# T05: persistence and API round trip
# ---------------------------------------------------------------------------

def _admin_payload(**overrides):
    values = {
        "provider_protocol": "openai_compatible",
        "provider_name": "test",
        "base_url": "https://capacity.test/v1",
        "model_name": "synthetic-large",
        "credential_env_ref": "TEST_KEY",
        "capabilities": {"text": True, "streaming": True},
        "context_window": 1_048_576,
        "max_output_tokens": 8_192,
        "max_attempts": 2,
    }
    values.update(overrides)
    return values


def _capacity_dict(**overrides):
    values = {
        "schema": "capacity-evidence-v1",
        "mode": "auto",
        "status": "verified",
        "source": "catalog",
        "effective_context_limit": 1_048_576,
        "model_context_limit": 1_048_576,
        "model_max_output_limit": 131_072,
        "counter_id": "synthetic-exact",
        "counter_version": "synthetic-exact-v1",
        "counter_mode": "verified",
    }
    values.update(overrides)
    return values


def test_model_admin_round_trips_capacity_fields(tmp_path, monkeypatch):
    import app.model_capacity as catalog
    monkeypatch.setattr(catalog, "CATALOG", (_verified_entry(),))
    from app.db import Database
    from app.model_admin import ModelAdminService

    service = ModelAdminService(Database(tmp_path / "agent.db"))
    created = service.create_profile({
        "name": "大窗口模型",
        **_admin_payload(capacity_evidence=_capacity_dict()),
    })
    version = created["versions"][0]
    assert version["working_window_mode"] == "auto"
    assert version["model_context_limit"] == 1_048_576
    assert version["model_max_output_limit"] == 131_072
    assert version["capacity_status"] == "verified"
    assert version["counter_mode"] == "verified"
    assert version["capacity"]["source"] == "catalog"
    assert isinstance(version["capacity_evidence"], str)

    reloaded = service.version(version["id"])
    assert reloaded["working_window_mode"] == "auto"
    assert reloaded["model_context_limit"] == 1_048_576
    assert reloaded["capacity_status"] == "verified"


def test_model_admin_rejects_unverified_auto_without_a_declared_fallback(tmp_path):
    from app.db import Database
    from app.model_admin import ModelAdminError, ModelAdminService

    service = ModelAdminService(Database(tmp_path / "agent.db"))
    with pytest.raises(ModelAdminError, match="verified capacity evidence"):
        service.create_profile({
            "name": "未验证",
            **_admin_payload(capacity_evidence=_capacity_dict(
                status="unverified", source="catalog-unverified",
                model_context_limit=None, effective_context_limit=None,
            )),
        })


def test_model_admin_accepts_only_internal_legacy_fallback(tmp_path, monkeypatch):
    from app.db import Database
    from app.model_admin import ModelAdminService, ModelAdminError
    from app.config import load_model_profile_from_env
    service = ModelAdminService(Database(tmp_path / "agent.db"))
    payload = {"name": "fallback", **_admin_payload(context_window=32768,
        capacity_evidence=_capacity_dict(status="unverified", source="legacy-conservative-default",
            model_context_limit=None, effective_context_limit=32768))}
    with pytest.raises(ModelAdminError, match="verified capacity evidence"):
        service.create_profile(payload)
    # A third-party endpoint has no catalog entry, so the environment loader
    # resolves the legacy conservative window; ensure_profile registers it
    # internally without exposing that path to the public API.
    _set_generic(monkeypatch, base_url="https://proxy.test/v1", model="deepseek-flash")
    bound = service.ensure_profile(load_model_profile_from_env())
    version = service.version(bound.registered_profile_version_id)
    assert version["capacity_source"] == "legacy-conservative-default"
    assert version["context_window"] == 32768


def test_model_admin_rejects_output_above_the_verified_model_limit(tmp_path, monkeypatch):
    import app.model_capacity as catalog
    monkeypatch.setattr(catalog, "CATALOG", (_verified_entry(),))
    from app.db import Database
    from app.model_admin import ModelAdminError, ModelAdminService

    service = ModelAdminService(Database(tmp_path / "agent.db"))
    with pytest.raises(ModelAdminError, match="model output limit"):
        service.create_profile({
            "name": "输出越界",
            **_admin_payload(
                max_output_tokens=200_000,
                capacity_evidence=_capacity_dict(),
            ),
        })


def test_model_admin_rejects_manual_window_above_the_model_capacity(tmp_path, monkeypatch):
    import app.model_capacity as catalog
    monkeypatch.setattr(catalog, "CATALOG", (_verified_entry(),))
    from app.db import Database
    from app.model_admin import ModelAdminError, ModelAdminService

    service = ModelAdminService(Database(tmp_path / "agent.db"))
    with pytest.raises(ModelAdminError, match="verified model capacity"):
        service.create_profile({
            "name": "手动越界",
            **_admin_payload(
                context_window=2_000_000,
                capacity_evidence=_capacity_dict(
                    mode="manual", status="manual", source="manual",
                    model_context_limit=1_048_576,
                ),
            ),
        })


def test_capacity_record_participates_in_the_version_digest(tmp_path, monkeypatch):
    import app.model_capacity as catalog
    monkeypatch.setattr(catalog, "CATALOG", (_verified_entry(),))
    from app.db import Database
    from app.model_admin import ModelAdminService

    service = ModelAdminService(Database(tmp_path / "agent.db"))
    profile = service.create_profile({
        "name": "摘要参与",
        **_admin_payload(capacity_evidence=_capacity_dict()),
    })
    first = profile["versions"][0]
    monkeypatch.setattr(catalog, "CATALOG", (_verified_entry(context_limit=524_288),))
    second = service.add_version(profile["id"], _admin_payload(
        context_window=524_288,
        capacity_evidence=_capacity_dict(model_context_limit=524_288, effective_context_limit=524_288),
    ))
    assert second["config_digest"] != first["config_digest"]


def test_capacity_record_for_profile_builds_from_resolved_fields():
    from app.model_gateway import ModelProfile
    from app.model_capacity import capacity_record_for_profile, loads_capacity_record

    profile = ModelProfile(
        "https://capacity.test/v1", "synthetic-large", "TEST_KEY",
        context_window=65_536, max_output_tokens=8_192,
        working_window_mode="manual", capacity_status="manual",
        capacity_source="manual", model_context_limit=1_048_576,
        model_max_output_limit=131_072, counter_id="utf8-upper-bound",
        counter_version="utf8-upper-bound-v1", counter_mode="estimate",
    )
    record = loads_capacity_record(capacity_record_for_profile(profile))
    assert record is not None
    assert record["mode"] == "manual"
    assert record["effective_context_limit"] == 65_536
    assert record["model_context_limit"] == 1_048_576


# ---------------------------------------------------------------------------
# T06: counter factory
# ---------------------------------------------------------------------------

class _Chars4Counter:
    version = "chars4-v1"

    def count_text(self, value: str) -> int:
        return max(len(value) // 4, 1)

    def count_payload(self, messages, tools=None) -> int:
        import json

        return self.count_text(json.dumps(
            {"messages": messages, "tools": tools or []},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))


def test_counter_factory_defaults_to_the_conservative_estimate():
    from app.model_gateway import ModelProfile
    from app.token_budget import (
        COUNTER_MODE_ESTIMATE, DEFAULT_COUNTER_ID, counter_for_profile,
    )

    profile = ModelProfile("https://example.test/v1", "demo", "TEST_KEY")
    selection = counter_for_profile(profile)
    assert selection.counter_id == DEFAULT_COUNTER_ID
    assert selection.mode == COUNTER_MODE_ESTIMATE
    assert "conservative" in selection.applicability


def test_registered_counter_switches_the_algorithm_and_the_recorded_version():
    from app.model_gateway import ModelProfile
    from app.token_budget import (
        COUNTER_MODE_ESTIMATE, _COUNTER_ADAPTERS, assert_request_fits,
        counter_for_profile, effective_input_budget, hot_window, register_token_counter,
    )

    counter = _Chars4Counter()
    register_token_counter("chars4", counter, mode=COUNTER_MODE_ESTIMATE, applicability="test only")
    try:
        profile = ModelProfile(
            "https://example.test/v1", "demo", "TEST_KEY",
            context_window=100_000, max_output_tokens=1_000, counter_id="chars4",
        )
        selection = counter_for_profile(profile)
        assert selection.counter is counter
        assert selection.counter_version == "chars4-v1"
        assert effective_input_budget(profile).counter_version == "chars4-v1"
        assert hot_window(profile).counter_version == "chars4-v1"

        messages = [{"role": "user", "content": "x" * 400}]
        counted = assert_request_fits(messages, None, profile)
        assert counted == counter.count_payload(messages, None)
    finally:
        _COUNTER_ADAPTERS.pop("chars4", None)


def test_verified_counter_without_evidence_falls_back_to_estimate():
    from app.model_gateway import ModelProfile
    from app.token_budget import (
        COUNTER_MODE_ESTIMATE, COUNTER_MODE_VERIFIED, _COUNTER_ADAPTERS,
        counter_for_profile, register_token_counter,
    )

    register_token_counter("exact-v1", _Chars4Counter(), mode=COUNTER_MODE_VERIFIED, applicability="synthetic")
    try:
        unverified = ModelProfile(
            "https://example.test/v1", "demo", "TEST_KEY", counter_id="exact-v1",
        )
        selection = counter_for_profile(unverified)
        assert selection.mode == COUNTER_MODE_ESTIMATE
        assert selection.counter_version == "utf8-upper-bound-v1"
        assert "no counter_evidence_version" in selection.applicability

        verified = ModelProfile(
            "https://example.test/v1", "demo", "TEST_KEY", counter_id="exact-v1",
            counter_evidence_version="synthetic-v1",
        )
        selection = counter_for_profile(verified)
        assert selection.mode == COUNTER_MODE_VERIFIED
        assert selection.counter_version == "chars4-v1"
    finally:
        _COUNTER_ADAPTERS.pop("exact-v1", None)


def test_gateway_exposes_the_same_counter_packing_uses():
    from app.model_gateway import ModelGateway, ModelProfile
    from app.token_budget import (
        COUNTER_MODE_ESTIMATE, _COUNTER_ADAPTERS, register_token_counter,
    )

    register_token_counter("chars4-gw", _Chars4Counter(), mode=COUNTER_MODE_ESTIMATE, applicability="test")
    try:
        gateway = ModelGateway(ModelProfile(
            "https://example.test/v1", "demo", "TEST_KEY",
            context_window=100_000, max_output_tokens=1_000, counter_id="chars4-gw",
        ))
        assert gateway.request_counter().version == "chars4-v1"
    finally:
        _COUNTER_ADAPTERS.pop("chars4-gw", None)


def test_unknown_counter_id_falls_back_with_a_reason():
    from app.model_gateway import ModelProfile
    from app.token_budget import counter_for_profile

    selection = counter_for_profile(ModelProfile(
        "https://example.test/v1", "demo", "TEST_KEY", counter_id="not-registered",
    ))
    assert selection.counter_version == "utf8-upper-bound-v1"
    assert "not-registered" in selection.applicability


# ---------------------------------------------------------------------------
# T08: compression follows the resolved window
# ---------------------------------------------------------------------------

def test_compression_thresholds_scale_with_the_resolved_window():
    from app.model_gateway import ModelProfile
    from app.token_budget import effective_input_budget, hot_window, static_archive_policy

    small = ModelProfile(
        "https://example.test/v1", "demo", "TEST_KEY",
        context_window=32_768, max_output_tokens=8_192,
    )
    large = ModelProfile(
        "https://example.test/v1", "demo", "TEST_KEY",
        context_window=1_048_576, max_output_tokens=8_192,
    )
    small_policy = static_archive_policy(small)
    large_policy = static_archive_policy(large)
    assert large_policy.trigger > small_policy.trigger * 10
    assert large_policy.target > small_policy.target * 10

    small_window = hot_window(small)
    large_window = hot_window(large)
    assert small_window.input_limit == effective_input_budget(small).input_limit
    assert large_window.input_limit == effective_input_budget(large).input_limit
    # The static line is derived from the same H, not from a fixed constant.
    assert large_window.archive_trigger == large_policy.trigger
    assert small_window.archive_trigger == small_policy.trigger
    # The protected recent-turn floor is unchanged by a larger window.
    assert large_window.min_turns == small_window.min_turns == 5


def test_hot_window_records_capacity_provenance():
    from app.model_gateway import ModelProfile
    from app.token_budget import hot_window

    window = hot_window(ModelProfile(
        "https://api.deepseek.com", "deepseek-flash", "TEST_KEY",
        context_window=32_768, max_output_tokens=8_192,
        working_window_mode="auto", capacity_status="unverified",
        capacity_source="legacy-conservative-default",
        model_context_limit=None, counter_id="utf8-upper-bound",
    ))
    view = window.public_view()
    assert view["working_window_mode"] == "auto"
    assert view["capacity_status"] == "unverified"
    assert view["capacity_source"] == "legacy-conservative-default"
    assert view["counter_id"] == "utf8-upper-bound"


# ---------------------------------------------------------------------------
# T10: migration
# ---------------------------------------------------------------------------

def _legacy_profile(tmp_path):
    from app.db import Database
    from app.model_admin import ModelAdminService

    service = ModelAdminService(Database(tmp_path / "agent.db"))
    created = service.create_profile({
        "name": "旧版模型",
        **_admin_payload(context_window=32_768, max_output_tokens=4_096),
    })
    return service, created


def test_migration_dry_run_reports_without_changing_the_version(tmp_path):
    from app.model_capacity_migration import plan_model_capacity_migration

    service, created = _legacy_profile(tmp_path)
    plans = plan_model_capacity_migration(service.db)
    assert len(plans) == 1
    item = plans[0]
    assert item["action"] == "dry-run"
    assert item["old_context_window"] == 32_768
    assert item["proposed_mode"] == "manual"
    assert item["proposed_effective_context_limit"] == 32_768
    # The immutable version is untouched by planning.
    assert service.version(created["versions"][0]["id"])["working_window_mode"] is None


def test_migration_apply_creates_one_new_version_and_is_idempotent(tmp_path):
    from app.model_capacity_migration import (
        apply_model_capacity_migration, plan_model_capacity_migration,
    )

    service, created = _legacy_profile(tmp_path)
    old_version_id = created["versions"][0]["id"]
    plans = plan_model_capacity_migration(service.db)
    report = apply_model_capacity_migration(service.db, plans, apply=True)
    assert report["created"] == 1
    assert report["items"][0]["apply_result"] == "created"

    new_version = service.version(report["items"][0]["new_version_id"])
    assert new_version["working_window_mode"] == "manual"
    assert new_version["context_window"] == 32_768
    assert new_version["capacity_status"] == "manual"
    # The original version keeps its old shape.
    assert service.version(old_version_id)["working_window_mode"] is None

    # Re-running finds the capacity record and skips.
    replans = plan_model_capacity_migration(service.db)
    assert replans[0]["action"] == "skip"
    second = apply_model_capacity_migration(service.db, replans, apply=True)
    assert second["created"] == 0 and second["skipped"] == 1


def test_migration_prefer_auto_keeps_the_manual_window_when_capacity_is_unverified(tmp_path):
    from app.model_capacity_migration import (
        apply_model_capacity_migration, plan_model_capacity_migration,
    )

    service, _created = _legacy_profile(tmp_path)
    plans = plan_model_capacity_migration(service.db, prefer_auto=True)
    item = plans[0]
    assert item["proposed_mode"] == "manual"
    assert item["proposed_effective_context_limit"] == 32_768
    assert item.get("auto_fallback_reason") == "catalog capacity is not verified"
    report = apply_model_capacity_migration(service.db, plans, apply=True)
    assert report["items"][0]["capacity_status"] == "manual"


def test_capacity_endpoint_resolves_official_deepseek_without_guessing(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.startup import build_runtime

    runtime = build_runtime(tmp_path)
    app = create_app(runtime=runtime)
    http = TestClient(app)
    headers = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000"}
    response = http.get("/api/model-capacity", headers=headers, params={
        "base_url": "https://api.deepseek.com",
        "protocol": "openai_compatible",
        "model": "deepseek-flash",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["capacity"]["status"] == CAPACITY_STATUS_OFFICIAL_DEFAULT
    assert body["capacity"]["effective_context_limit"] == 1_000_000
    assert body["entry"]["max_output_limit"] == 393_216
    assert body["entry"]["context_limit"] is None
    assert body["entry"]["default_context_limit"] == 1_000_000

    missing = http.get("/api/model-capacity", headers=headers, params={
        "base_url": "https://proxy.test/v1", "model": "deepseek-flash",
    })
    assert missing.status_code == 200
    assert missing.json()["capacity"]["source"] == "catalog-miss"


def test_capacity_record_round_trips_and_legacy_text_is_not_mistaken_for_a_record():
    resolution = resolve_working_window(
        base_url="https://api.deepseek.com",
        protocol="openai_compatible",
        model_id="deepseek-flash",
        mode="auto",
        entries=CATALOG,
    )
    stored = dumps_capacity_record(resolution.capacity_record())
    parsed = loads_capacity_record(stored)
    assert parsed is not None
    assert parsed["status"] == CAPACITY_STATUS_OFFICIAL_DEFAULT
    assert parsed["model_max_output_limit"] == 393_216
    assert loads_capacity_record("official pricing snapshot, plain note") is None
    assert loads_capacity_record(None) is None
