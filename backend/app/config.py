from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


_LOADED_CREDENTIALS: dict[str, str] = {}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def monetary_limits_enabled() -> bool:
    return os.getenv("BETTER_AGENT_COST_MODE", "observe").lower() == "enforce"


def load_user_model_environment() -> tuple[str, ...]:
    """Import Windows user settings at launch, not inside tests or model calls."""
    if os.name != "nt":
        return ()
    import winreg
    names = (
        "AGENT_MODEL_PROVIDER", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
        "EMBEDDING_API_KEY_ENV", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS",
        "EMBEDDING_TIMEOUT_SECONDS", "MEMORY_EMBEDDING_API_KEY", "EMBEDDING_API_KEY",
        "MEMORY_EMBEDDING_API_KEY_ENV", "MEMORY_EMBEDDING_BASE_URL", "MEMORY_EMBEDDING_MODEL",
        "MEMORY_EMBEDDING_DIMENSIONS", "MEMORY_EMBEDDING_TIMEOUT_SECONDS",
        "MEMORY_REFERENCE_MODE", "MEMORY_REFERENCE_HISTORY_TURNS",
    )
    loaded = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in names:
                if name in os.environ:
                    continue
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if isinstance(value, str) and value:
                    os.environ[name] = value
                    loaded.append(name)
            # Import only explicitly selected embedding credentials, never scan
            # the registry for arbitrary provider secrets. Process/.env wins.
            for selector in ("EMBEDDING_API_KEY_ENV", "MEMORY_EMBEDDING_API_KEY_ENV"):
                name = (os.getenv(selector) or "").strip()
                if not _ENV_NAME.fullmatch(name) or name in os.environ or name.startswith("AGENT_FALLBACK_MODEL_"):
                    continue
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if isinstance(value, str) and value:
                    os.environ[name] = value
                    loaded.append(name)
    except FileNotFoundError:
        pass
    return tuple(loaded)


def resolve_credential(api_key_env: str) -> str | None:
    """Resolve process configuration without leaking file credentials globally."""
    return os.getenv(api_key_env) or _LOADED_CREDENTIALS.get(api_key_env)


def load_env_file(path: str | Path, *, override: bool = False) -> tuple[str, ...]:
    """Load a dotenv file without executing or interpolating its contents."""
    env_path = Path(path)
    if not env_path.is_file():
        return ()
    loaded: list[str] = []
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_NAME.fullmatch(key):
            raise ValueError(f"invalid .env entry at line {line_number}")
        value = raw_value.strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ValueError(f"unterminated quoted .env value at line {line_number}")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)


@dataclass(frozen=True)
class AppConfig:
    version: str = "1.0.0"
    api_key_env: str = "AGENT_MODEL_API_KEY"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")

    @property
    def api_key_configured(self) -> bool:
        return bool(
            os.getenv(self.api_key_env)
            or any(os.getenv(name) for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"))
        )

    def public_view(self) -> dict[str, object]:
        return {
            "status": "ok",
            "service": "better-agent",
            "version": self.version,
            "api_key_configured": self.api_key_configured,
        }


@dataclass(frozen=True)
class ProviderPreset:
    api_key_env: str
    base_url: str
    model: str
    provider_protocol: str
    context_window: int = 32768
    max_output_tokens: int = 8192


@dataclass(frozen=True)
class ModelPriceConfig:
    uncached_input_rate: int
    cache_read_rate: int
    cache_write_rate: int
    output_rate: int
    reasoning_rate: int
    effective_at: str
    source_url: str


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-5.6-luna", "openai_compatible"),
    "anthropic": ProviderPreset("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1", "claude-sonnet-5", "anthropic"),
    "deepseek": ProviderPreset("DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-flash", "openai_compatible"),
}

# USD microunits per one million tokens. DeepSeek uses the peak rate so the
# pre-call reservation never understates its time-varying fee.
BUILTIN_MODEL_PRICES: dict[tuple[str, str], ModelPriceConfig] = {
    ("openai", "gpt-5.6-luna"): ModelPriceConfig(
        200_000, 20_000, 250_000, 1_200_000, 0,
        "2026-07-30T00:00:00+00:00", "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
    ),
    ("anthropic", "claude-sonnet-5"): ModelPriceConfig(
        2_000_000, 200_000, 2_500_000, 10_000_000, 0,
        "2026-09-01T00:00:00+00:00", "https://platform.claude.com/docs/en/about-claude/pricing",
    ),
    ("deepseek", "deepseek-flash"): ModelPriceConfig(
        300_000, 6_000, 0, 1_200_000, 0,
        "2026-09-11T00:00:00+00:00", "https://api-docs.deepseek.com/quick_start/pricing/",
    ),
}


def _resolve_window_contract(
    prefix: str,
    *,
    base_url: str,
    protocol: str,
    model: str,
    default_context_window: int,
    max_output_tokens: int,
    contract: dict,
    explicit_window: int | None = None,
    explicit_source: str | None = None,
) -> dict:
    """Resolve the working window for one configuration entry point (T04).

    Priority: explicit ``{PREFIX}_CONTEXT_WINDOW`` (manual) > verified catalog
    auto > legacy conservative default. ``{PREFIX}_WORKING_WINDOW_MODE`` may
    force ``auto``/``manual``; an explicit window or a ``soft_context_limit``
    implies manual. Auto without verified capacity keeps the legacy window and
    labels it ``legacy-conservative-default`` instead of inheriting an
    unverified large capacity.
    """
    from .model_capacity import (
        CATALOG,
        CAPACITY_STATUS_VERIFIED,
        WORKING_WINDOW_AUTO,
        WORKING_WINDOW_MANUAL,
        dumps_capacity_record,
        resolve_working_window,
    )

    explicit = explicit_window if explicit_window is not None else _optional_positive_int_env(f"{prefix}_CONTEXT_WINDOW")
    mode_env = (os.getenv(f"{prefix}_WORKING_WINDOW_MODE") or "").strip().lower()
    if mode_env and mode_env not in {WORKING_WINDOW_AUTO, WORKING_WINDOW_MANUAL}:
        raise ValueError(f"{prefix}_WORKING_WINDOW_MODE must be auto or manual")
    manual_intent = explicit is not None or contract.get("soft_context_limit") is not None
    mode = WORKING_WINDOW_MANUAL if explicit_window is not None else (mode_env or (WORKING_WINDOW_MANUAL if manual_intent else WORKING_WINDOW_AUTO))
    resolution = resolve_working_window(
        base_url=base_url,
        protocol=protocol,
        model_id=model,
        mode=mode,
        explicit_context_window=explicit,
        admitted_context_limit=contract.get("admitted_context_limit"),
        soft_context_limit=contract.get("soft_context_limit"),
        max_output_tokens=max_output_tokens,
        entries=CATALOG,
    )
    record = resolution.capacity_record()
    if resolution.effective_context_limit is not None:
        context_window = resolution.effective_context_limit
        source = explicit_source or resolution.source
    else:
        context_window = int(default_context_window)
        source = "legacy-conservative-default"
        # The fallback window is what this profile will actually use, so the
        # persisted record must state it rather than leaving the window blank.
        record["effective_context_limit"] = context_window
        record["source"] = source
    record["source"] = source
    kwargs: dict[str, object] = {
        "context_window": context_window,
        "working_window_mode": resolution.mode,
        "model_context_limit": resolution.model_context_limit,
        "model_max_output_limit": resolution.model_max_output_limit,
        "capacity_status": resolution.status,
        "capacity_source": source,
        "counter_id": resolution.counter_id,
        "counter_version": resolution.counter_version,
        "counter_mode": resolution.counter_mode,
        "soft_context_limit": resolution.soft_context_limit,
    }
    kwargs["capacity_evidence"] = dumps_capacity_record(record)
    if resolution.status == CAPACITY_STATUS_VERIFIED:
        kwargs["context_window_verified"] = True
    return kwargs


def load_llm_ap(path: str | Path, api_key_env: str = "AGENT_MODEL_API_KEY", model_id: str | None = None):
    """Load the legacy live-test file without returning or exporting the secret."""
    values: dict[str, str] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in {
            "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID", "LLM_MODEL_IDS",
            "LLM_CONTEXT_WINDOW", "LLM_MAX_OUTPUT_TOKENS", "API_KEY", "BASE_URL", "MODEL_ID",
        }:
            raise ValueError("invalid LLM_AP entry")
        values[{"API_KEY": "LLM_API_KEY", "BASE_URL": "LLM_BASE_URL", "MODEL_ID": "LLM_MODEL_ID"}.get(key, key)] = value
    if not values.get("LLM_API_KEY") or not values.get("LLM_BASE_URL"):
        raise ValueError("LLM_AP must define an API key and base URL")
    configured_model = model_id or values.get("LLM_MODEL_ID") or next(
        (item.strip() for item in values.get("LLM_MODEL_IDS", "").split(",") if item.strip()), None
    )
    if not configured_model:
        raise ValueError("LLM_AP must define or receive a model ID")
    _LOADED_CREDENTIALS[api_key_env] = values["LLM_API_KEY"]
    from .model_gateway import ModelProfile

    base_url = values["LLM_BASE_URL"].rstrip("/")
    if base_url.endswith("/models"):
        base_url = base_url[:-7]
    try:
        max_output_tokens = int(values.get("LLM_MAX_OUTPUT_TOKENS", "8192"))
        explicit_context = values.get("LLM_CONTEXT_WINDOW")
        explicit_context = int(explicit_context) if explicit_context else None
    except ValueError as exc:
        raise ValueError("LLM context budget must use integers") from exc
    contract = _budget_contract_from_env("AGENT_MODEL")
    protocol = os.getenv("AGENT_MODEL_PROVIDER_PROTOCOL", "openai_compatible")
    window = _resolve_window_contract(
        "AGENT_MODEL", base_url=base_url, protocol=protocol, model=configured_model,
        default_context_window=32768,
        max_output_tokens=max_output_tokens, contract=contract,
        explicit_window=explicit_context,
        explicit_source="llm-file" if explicit_context is not None else None,
    )
    context_window = int(window["context_window"])
    _validate_context_budget(context_window, max_output_tokens)
    provider_name = _provider_from_host(base_url) or os.getenv("AGENT_MODEL_PROVIDER_NAME", "openai-compatible")
    return ModelProfile(
        base_url=base_url,
        model=configured_model,
        api_key_env=api_key_env,
        provider_protocol=protocol,
        provider_name=provider_name,
        max_output_tokens=max_output_tokens,
        **{**contract, **window},
    )


def load_model_profile_from_env(prefix: str = "AGENT_MODEL"):
    """Load one explicitly configured generic model profile."""
    base_url = os.getenv(f"{prefix}_BASE_URL")
    model = os.getenv(f"{prefix}_ID")
    api_key_env = os.getenv(f"{prefix}_API_KEY_ENV", f"{prefix}_API_KEY")
    if not base_url or not model:
        raise ValueError(f"{prefix}_BASE_URL and {prefix}_ID are required")
    if not resolve_credential(api_key_env):
        raise ValueError(f"model key is not configured in {api_key_env}")
    from .model_gateway import ModelProfile

    base_url = base_url.rstrip("/")
    protocol = os.getenv(f"{prefix}_PROVIDER_PROTOCOL", "openai_compatible")
    max_output_tokens = _positive_int_env(f"{prefix}_MAX_OUTPUT_TOKENS", 8192)
    contract = _budget_contract_from_env(prefix)
    window = _resolve_window_contract(
        prefix, base_url=base_url, protocol=protocol, model=model,
        default_context_window=32768, max_output_tokens=max_output_tokens, contract=contract,
    )
    context_window = int(window["context_window"])
    _validate_context_budget(context_window, max_output_tokens)
    return ModelProfile(
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=float(os.getenv(f"{prefix}_TIMEOUT_SECONDS", "60")),
        max_attempts=_positive_int_env(f"{prefix}_MAX_ATTEMPTS", 4),
        network_retries=int(os.getenv(f"{prefix}_NETWORK_RETRIES", "2")),
        provider_protocol=protocol,
        provider_name=os.getenv(f"{prefix}_PROVIDER_NAME", _provider_from_host(base_url) or "openai-compatible"),
        max_output_tokens=max_output_tokens,
        **{**contract, **window},
    )


def load_model_profile_from_environment():
    """Prefer generic AGENT_MODEL_*, otherwise select one standard vendor key."""
    if any(os.getenv(name) for name in ("AGENT_MODEL_BASE_URL", "AGENT_MODEL_ID", "AGENT_MODEL_API_KEY")):
        return load_model_profile_from_env()
    requested = os.getenv("AGENT_MODEL_PROVIDER", "").strip().lower()
    available = [name for name, preset in PROVIDER_PRESETS.items() if os.getenv(preset.api_key_env)]
    if requested:
        if requested not in PROVIDER_PRESETS:
            raise ValueError("AGENT_MODEL_PROVIDER must be openai, anthropic, or deepseek")
        if requested not in available:
            raise ValueError(f"{PROVIDER_PRESETS[requested].api_key_env} is required")
        provider = requested
    elif len(available) == 1:
        provider = available[0]
    elif len(available) > 1:
        raise ValueError("multiple provider keys are configured; set AGENT_MODEL_PROVIDER")
    else:
        return None
    preset = PROVIDER_PRESETS[provider]
    env_prefix = provider.upper()
    from .model_gateway import ModelProfile

    base_url = os.getenv(f"{env_prefix}_BASE_URL", preset.base_url).rstrip("/")
    model = os.getenv(f"{env_prefix}_MODEL", preset.model)
    max_output_tokens = _positive_int_env(f"{env_prefix}_MAX_OUTPUT_TOKENS", preset.max_output_tokens)
    contract = _budget_contract_from_env(env_prefix)
    window = _resolve_window_contract(
        env_prefix, base_url=base_url, protocol=preset.provider_protocol, model=model,
        default_context_window=preset.context_window, max_output_tokens=max_output_tokens,
        contract=contract,
    )
    context_window = int(window["context_window"])
    _validate_context_budget(context_window, max_output_tokens)
    return ModelProfile(
        base_url=base_url,
        model=model,
        api_key_env=preset.api_key_env,
        timeout_seconds=float(os.getenv(f"{env_prefix}_TIMEOUT_SECONDS", "60")),
        max_attempts=_positive_int_env(f"{env_prefix}_MAX_ATTEMPTS", 4),
        network_retries=int(os.getenv(f"{env_prefix}_NETWORK_RETRIES", "2")),
        provider_protocol=preset.provider_protocol,
        provider_name=provider,
        max_output_tokens=max_output_tokens,
        declared_capabilities=frozenset({"streaming", "tool_calling", "json_object"}),
        **{**contract, **window},
    )


def load_model_price(profile, prefix: str = "AGENT_MODEL") -> ModelPriceConfig | None:
    """Load a complete explicit price or a reviewed first-party preset."""
    names = {
        "uncached_input_rate": f"{prefix}_PRICE_UNCACHED_INPUT",
        "cache_read_rate": f"{prefix}_PRICE_CACHE_READ",
        "cache_write_rate": f"{prefix}_PRICE_CACHE_WRITE",
        "output_rate": f"{prefix}_PRICE_OUTPUT",
        "reasoning_rate": f"{prefix}_PRICE_REASONING",
    }
    explicit = [name for name in names.values() if os.getenv(name) is not None]
    if explicit:
        missing = [name for name in names.values() if os.getenv(name) is None]
        source = os.getenv(f"{prefix}_PRICE_SOURCE_URL", "").strip()
        effective_at = os.getenv(f"{prefix}_PRICE_EFFECTIVE_AT", "").strip()
        if missing or not source or not effective_at:
            raise ValueError("model price requires all five rates, PRICE_SOURCE_URL, and PRICE_EFFECTIVE_AT")
        rates: dict[str, int] = {}
        for field, name in names.items():
            try:
                rates[field] = int(os.environ[name])
            except ValueError as exc:
                raise ValueError(f"{name} must be a non-negative integer") from exc
            if rates[field] < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        return ModelPriceConfig(**rates, effective_at=effective_at, source_url=source)
    provider = profile.provider_name.lower()
    if provider == "openai-compatible":
        provider = _provider_from_host(profile.base_url) or provider
    preset = BUILTIN_MODEL_PRICES.get((provider, profile.model))
    first_party_host = {
        "openai": "api.openai.com", "anthropic": "api.anthropic.com", "deepseek": "api.deepseek.com",
    }.get(provider)
    return preset if preset and urlsplit(profile.base_url).hostname == first_party_host else None


def _provider_from_host(base_url: str) -> str | None:
    return {
        "api.openai.com": "openai",
        "api.anthropic.com": "anthropic",
        "api.deepseek.com": "deepseek",
    }.get(urlsplit(base_url).hostname)


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_positive_int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_text_env(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    return raw.strip() or None


def _non_negative_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _protocol_budget_from_env(prefix: str):
    """Adapter wrapper budget. Without recorded provenance the profile has none."""
    evidence = _optional_text_env(f"{prefix}_PROTOCOL_BUDGET_EVIDENCE")
    if evidence is None:
        return None
    from .token_budget import ProtocolBudget

    return ProtocolBudget(
        b0=_non_negative_int_env(f"{prefix}_PROTOCOL_BUDGET_B0", 0),
        per_message=_non_negative_int_env(f"{prefix}_PROTOCOL_BUDGET_PER_MESSAGE", 0),
        per_tool_definition=_non_negative_int_env(f"{prefix}_PROTOCOL_BUDGET_PER_TOOL_DEFINITION", 0),
        per_tool_call=_non_negative_int_env(f"{prefix}_PROTOCOL_BUDGET_PER_TOOL_CALL", 0),
        per_tool_result=_non_negative_int_env(f"{prefix}_PROTOCOL_BUDGET_PER_TOOL_RESULT", 0),
        evidence=evidence,
    )


def _budget_contract_from_env(prefix: str) -> dict[str, object]:
    """R1 budget contract fields.

    Leaving ``{prefix}_VALIDATION_TIER`` unset keeps the profile on the legacy
    ``C - O - margin`` formula. Declaring ``A`` opts into the evidenced path,
    where ``admitted_context_limit`` and ``counter_evidence_version`` become
    mandatory and the repository's default window stops being capacity evidence.
    """
    tier = _optional_text_env(f"{prefix}_VALIDATION_TIER")
    if tier is not None:
        tier = tier.upper()
        if tier not in {"A", "B"}:
            raise ValueError(f"{prefix}_VALIDATION_TIER must be A or B")
    verified = (os.getenv(f"{prefix}_CONTEXT_WINDOW_VERIFIED", "") or "").strip().lower()
    return {
        "validation_tier": tier,
        "admitted_context_limit": _optional_positive_int_env(f"{prefix}_ADMITTED_CONTEXT_LIMIT"),
        "soft_context_limit": _optional_positive_int_env(f"{prefix}_SOFT_CONTEXT_LIMIT"),
        "context_window_verified": verified in {"1", "true", "yes", "on"},
        "counter_id": _optional_text_env(f"{prefix}_COUNTER_ID") or "utf8-upper-bound",
        "counter_version": _optional_text_env(f"{prefix}_COUNTER_VERSION") or "utf8-upper-bound-v1",
        "counter_evidence_version": _optional_text_env(f"{prefix}_COUNTER_EVIDENCE_VERSION"),
        "capacity_evidence": _optional_text_env(f"{prefix}_CAPACITY_EVIDENCE"),
        "protocol_budget": _protocol_budget_from_env(prefix),
        "history_min_turns": _optional_non_negative_int_env(f"{prefix}_HISTORY_MIN_TURNS"),
        "compact_ratio": _optional_ratio_env(f"{prefix}_COMPACT_RATIO"),
        "recent_window_bytes": _optional_non_negative_int_env(f"{prefix}_RECENT_WINDOW_BYTES"),
        "recent_window_ratio": _optional_ratio_env(f"{prefix}_RECENT_WINDOW_RATIO"),
        "archive_trigger_ratio": _optional_ratio_env(f"{prefix}_ARCHIVE_TRIGGER_RATIO"),
        "archive_reserve_ratio": _optional_ratio_env(f"{prefix}_ARCHIVE_RESERVE_RATIO"),
        "archive_prefix_reserve": _optional_non_negative_int_env(f"{prefix}_ARCHIVE_PREFIX_RESERVE"),
    }


def _optional_non_negative_int_env(name: str) -> int | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_ratio_env(name: str) -> float | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be in (0, 1]") from exc
    if not 0 < value <= 1:
        raise ValueError(f"{name} must be in (0, 1]")
    return value


def _validate_context_budget(context_window: int, max_output_tokens: int) -> None:
    if context_window <= 0 or max_output_tokens <= 0 or max_output_tokens >= context_window:
        raise ValueError("LLM context budget is invalid")
