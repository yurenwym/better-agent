"""Model capacity catalog and working-window resolution (T02/T03).

The working window is *resolved*, never assumed:

    W = min(verified model capacity, applicable endpoint limit, optional manual cap)

and the input budget H is then derived from W by the existing budget contract
(``token_budget.effective_input_budget``), which already subtracts the current
output reserve and safety margin.

Nothing in this module invents a capacity integer. An entry whose context length
is not backed by official evidence stays ``context_limit=None`` and resolves to
``unverified``: callers must keep the legacy conservative behaviour instead of
pretending the endpoint has a large verified window.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Iterable
from urllib.parse import urlsplit


CATALOG_VERSION = "model-capacity-2026-09-19"

WORKING_WINDOW_AUTO = "auto"
WORKING_WINDOW_MANUAL = "manual"

CAPACITY_STATUS_VERIFIED = "verified"
CAPACITY_STATUS_UNVERIFIED = "unverified"
CAPACITY_STATUS_MANUAL = "manual"
CAPACITY_STATUS_LEGACY = "legacy"
# An official vendor page declares a context length without an exact integer.
# The declared value is adopted as a conservative integer default and is *not*
# presented as a verified exact boundary.
CAPACITY_STATUS_OFFICIAL_DEFAULT = "official-default"
CAPACITY_SOURCE_OFFICIAL_DEFAULT = "catalog-official-default"

COUNTER_MODE_ESTIMATE = "estimate"
COUNTER_MODE_VERIFIED = "verified"


class CapacityContractError(ValueError):
    """A requested working window contradicts known, evidenced limits."""

    code = "invalid_capacity_contract"


@dataclass(frozen=True)
class CapacityEvidence:
    urls: tuple[str, ...]
    fetched_at: str
    snapshot_sha256: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class CapacityEntry:
    provider: str
    base_url: str
    protocol: str
    model_id: str
    model_version: str | None
    aliases: tuple[str, ...]
    context_limit: int | None
    max_output_limit: int | None
    counter_id: str
    counter_version: str
    counter_verified: bool
    context_verified: bool
    verified_at: str
    evidence: CapacityEvidence
    catalog_version: str = CATALOG_VERSION
    # Official vendor page declares a context length (e.g. "1M") without an
    # exact integer. The adopted conservative integer is a *default*, not a
    # verified boundary; third-party endpoints never inherit it.
    default_context_limit: int | None = None

    @property
    def verified(self) -> bool:
        return bool(self.context_verified and self.context_limit and self.context_limit > 0)

    def public_view(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "protocol": self.protocol,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "aliases": list(self.aliases),
            "context_limit": self.context_limit,
            "default_context_limit": self.default_context_limit,
            "max_output_limit": self.max_output_limit,
            "counter_id": self.counter_id,
            "counter_version": self.counter_version,
            "counter_verified": self.counter_verified,
            "context_verified": self.context_verified,
            "verified_at": self.verified_at,
            "catalog_version": self.catalog_version,
            "evidence": {
                "urls": list(self.evidence.urls),
                "fetched_at": self.evidence.fetched_at,
                "snapshot_sha256": list(self.evidence.snapshot_sha256),
                "notes": self.evidence.notes,
            },
        }


@dataclass(frozen=True)
class WorkingWindowResolution:
    mode: str
    status: str
    source: str
    effective_context_limit: int | None
    model_context_limit: int | None
    model_max_output_limit: int | None
    admitted_context_limit: int | None
    soft_context_limit: int | None
    counter_id: str
    counter_version: str
    counter_mode: str
    evidence_refs: tuple[str, ...]
    catalog_version: str | None
    verified_at: str | None
    entry: CapacityEntry | None = None
    reason: str = ""
    default_context_limit: int | None = None

    @property
    def verified(self) -> bool:
        return self.status == CAPACITY_STATUS_VERIFIED

    def public_view(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "status": self.status,
            "source": self.source,
            "effective_context_limit": self.effective_context_limit,
            "model_context_limit": self.model_context_limit,
            "model_max_output_limit": self.model_max_output_limit,
            "default_context_limit": self.default_context_limit,
            "admitted_context_limit": self.admitted_context_limit,
            "soft_context_limit": self.soft_context_limit,
            "counter_id": self.counter_id,
            "counter_version": self.counter_version,
            "counter_mode": self.counter_mode,
            "evidence_refs": list(self.evidence_refs),
            "catalog_version": self.catalog_version,
            "verified_at": self.verified_at,
            "reason": self.reason,
        }

    def capacity_record(self) -> dict[str, Any]:
        """The subset persisted in the profile's ``capacity_evidence`` column."""
        return {
            "schema": "capacity-evidence-v1",
            "mode": self.mode,
            "status": self.status,
            "source": self.source,
            "effective_context_limit": self.effective_context_limit,
            "model_context_limit": self.model_context_limit,
            "model_max_output_limit": self.model_max_output_limit,
            "default_context_limit": self.default_context_limit,
            "admitted_context_limit": self.admitted_context_limit,
            "soft_context_limit": self.soft_context_limit,
            "counter_id": self.counter_id,
            "counter_version": self.counter_version,
            "counter_mode": self.counter_mode,
            "evidence_refs": list(self.evidence_refs),
            "catalog_version": self.catalog_version,
            "verified_at": self.verified_at,
            "reason": self.reason,
        }


def normalize_endpoint(base_url: str) -> str:
    """Canonical endpoint key.

    Different proxies, tenant paths and protocols must not collapse into one
    entry, so the path is preserved (``/anthropic`` stays distinct) and only
    scheme/host case, default ports and a trailing slash are normalized.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url is required")
    parts = urlsplit(base_url.strip())
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("base_url must be an absolute URL")
    scheme = (parts.scheme or "https").lower()
    default_port = 443 if scheme == "https" else 80
    netloc = host if parts.port in (None, default_port) else f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def find_capacity_entry(
    base_url: str,
    protocol: str,
    model_id: str | None,
    *,
    entries: Iterable[CapacityEntry] | None = None,
) -> CapacityEntry | None:
    if not model_id or not str(model_id).strip():
        return None
    try:
        endpoint = normalize_endpoint(base_url)
    except ValueError:
        return None
    model = str(model_id).strip()
    for entry in CATALOG if entries is None else entries:
        if entry.protocol != protocol:
            continue
        if normalize_endpoint(entry.base_url) != endpoint:
            continue
        if model == entry.model_id or model in entry.aliases:
            return entry
    return None


def resolve_working_window(
    *,
    base_url: str,
    protocol: str,
    model_id: str | None,
    mode: str = WORKING_WINDOW_AUTO,
    explicit_context_window: int | None = None,
    admitted_context_limit: int | None = None,
    soft_context_limit: int | None = None,
    max_output_tokens: int | None = None,
    entries: Iterable[CapacityEntry] | None = None,
) -> WorkingWindowResolution:
    """Resolve one effective W and its provenance.

    ``explicit_context_window`` is the user's manual cap. ``soft_context_limit``
    is accepted as the persisted form of that cap so both channels cannot
    disagree: if both are present they must be equal.
    """
    if mode not in {WORKING_WINDOW_AUTO, WORKING_WINDOW_MANUAL}:
        raise CapacityContractError(f"unknown working window mode: {mode!r}")
    if explicit_context_window is not None and soft_context_limit is not None:
        if int(explicit_context_window) != int(soft_context_limit):
            raise CapacityContractError(
                "explicit context window and soft_context_limit disagree"
            )
    manual_cap = explicit_context_window if explicit_context_window is not None else soft_context_limit
    if manual_cap is not None and (isinstance(manual_cap, bool) or int(manual_cap) <= 0):
        raise CapacityContractError("manual context window must be a positive integer")
    if admitted_context_limit is not None and int(admitted_context_limit) <= 0:
        raise CapacityContractError("admitted_context_limit must be a positive integer")

    entry = find_capacity_entry(base_url, protocol, model_id, entries=entries)
    model_context = entry.context_limit if entry is not None and entry.context_verified else None
    model_output = entry.max_output_limit if entry is not None else None
    counter_id = entry.counter_id if entry is not None else "utf-8-upper-bound"
    counter_version = entry.counter_version if entry is not None else "utf8-upper-bound-v1"
    counter_mode = (
        COUNTER_MODE_VERIFIED if entry is not None and entry.counter_verified else COUNTER_MODE_ESTIMATE
    )
    evidence_refs = tuple(entry.evidence.urls) if entry is not None else ()
    catalog_version = entry.catalog_version if entry is not None else None
    verified_at = entry.verified_at if entry is not None else None

    if max_output_tokens is not None:
        if isinstance(max_output_tokens, bool) or int(max_output_tokens) <= 0:
            raise CapacityContractError("max_output_tokens must be a positive integer")
        if model_output is not None and int(max_output_tokens) > model_output:
            raise CapacityContractError(
                f"max_output_tokens {max_output_tokens} exceeds the verified model "
                f"output limit {model_output}"
            )

    manual_requested = mode == WORKING_WINDOW_MANUAL or manual_cap is not None
    if manual_requested:
        if manual_cap is None:
            raise CapacityContractError("manual mode requires an explicit context window")
        limits = [int(manual_cap)]
        if model_context is not None:
            limits.append(int(model_context))
        if admitted_context_limit is not None:
            limits.append(int(admitted_context_limit))
        if soft_context_limit is not None:
            limits.append(int(soft_context_limit))
        effective = min(limits)
        if model_context is not None and int(manual_cap) > int(model_context):
            raise CapacityContractError(
                f"manual context window {manual_cap} exceeds the verified model capacity {model_context}"
            )
        if admitted_context_limit is not None and int(manual_cap) > int(admitted_context_limit):
            raise CapacityContractError(
                f"manual context window {manual_cap} exceeds the endpoint limit {admitted_context_limit}"
            )
        if max_output_tokens is not None and int(max_output_tokens) >= effective:
            raise CapacityContractError("output reserve must be smaller than the working window")
        return WorkingWindowResolution(
            mode=WORKING_WINDOW_MANUAL,
            status=CAPACITY_STATUS_MANUAL,
            source="manual",
            effective_context_limit=effective,
            model_context_limit=model_context,
            model_max_output_limit=model_output,
            admitted_context_limit=admitted_context_limit,
            soft_context_limit=int(manual_cap),
            counter_id=counter_id,
            counter_version=counter_version,
            counter_mode=counter_mode,
            evidence_refs=evidence_refs,
            catalog_version=catalog_version,
            verified_at=verified_at,
            entry=entry,
        )

    # Auto mode.
    if entry is None:
        return WorkingWindowResolution(
            mode=WORKING_WINDOW_AUTO,
            status=CAPACITY_STATUS_UNVERIFIED,
            source="catalog-miss",
            effective_context_limit=None,
            model_context_limit=None,
            model_max_output_limit=None,
            admitted_context_limit=admitted_context_limit,
            soft_context_limit=soft_context_limit,
            counter_id=counter_id,
            counter_version=counter_version,
            counter_mode=counter_mode,
            evidence_refs=evidence_refs,
            catalog_version=catalog_version,
            verified_at=None,
            reason="no verified capacity entry matches this endpoint, protocol and model",
        )
    if not entry.verified and entry.default_context_limit:
        # Official page declares a context length (e.g. "1M") without an exact
        # integer. Adopt the declared value as a conservative default; it is
        # recorded as ``official-default``, never as ``verified``.
        limits = [int(entry.default_context_limit)]
        if admitted_context_limit is not None:
            limits.append(int(admitted_context_limit))
        if soft_context_limit is not None:
            limits.append(int(soft_context_limit))
        effective = min(limits)
        if max_output_tokens is not None and int(max_output_tokens) >= effective:
            raise CapacityContractError("output reserve must be smaller than the working window")
        return WorkingWindowResolution(
            mode=WORKING_WINDOW_AUTO,
            status=CAPACITY_STATUS_OFFICIAL_DEFAULT,
            source=CAPACITY_SOURCE_OFFICIAL_DEFAULT,
            effective_context_limit=effective,
            model_context_limit=None,
            model_max_output_limit=model_output,
            admitted_context_limit=admitted_context_limit,
            soft_context_limit=soft_context_limit,
            counter_id=counter_id,
            counter_version=counter_version,
            counter_mode=counter_mode,
            evidence_refs=evidence_refs,
            catalog_version=catalog_version,
            verified_at=verified_at,
            entry=entry,
            default_context_limit=entry.default_context_limit,
            reason=(
                "official vendor page declares this context length; adopted as a "
                "conservative integer default, not an exact verified boundary"
            ),
        )
    if not entry.verified:
        return WorkingWindowResolution(
            mode=WORKING_WINDOW_AUTO,
            status=CAPACITY_STATUS_UNVERIFIED,
            source="catalog-unverified",
            effective_context_limit=None,
            model_context_limit=None,
            model_max_output_limit=model_output,
            admitted_context_limit=admitted_context_limit,
            soft_context_limit=soft_context_limit,
            counter_id=counter_id,
            counter_version=counter_version,
            counter_mode=counter_mode,
            evidence_refs=evidence_refs,
            catalog_version=catalog_version,
            verified_at=verified_at,
            entry=entry,
            reason="the catalog entry has no verified exact context length",
        )
    limits = [int(entry.context_limit)]
    if admitted_context_limit is not None:
        limits.append(int(admitted_context_limit))
    if soft_context_limit is not None:
        limits.append(int(soft_context_limit))
    effective = min(limits)
    if max_output_tokens is not None and int(max_output_tokens) >= effective:
        raise CapacityContractError("output reserve must be smaller than the working window")
    return WorkingWindowResolution(
        mode=WORKING_WINDOW_AUTO,
        status=CAPACITY_STATUS_VERIFIED,
        source="catalog",
        effective_context_limit=effective,
        model_context_limit=entry.context_limit,
        model_max_output_limit=entry.max_output_limit,
        admitted_context_limit=admitted_context_limit,
        soft_context_limit=soft_context_limit,
        counter_id=counter_id,
        counter_version=counter_version,
        counter_mode=counter_mode,
        evidence_refs=evidence_refs,
        catalog_version=catalog_version,
        verified_at=verified_at,
        entry=entry,
    )


def dumps_capacity_record(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def capacity_record_for_profile(profile: Any) -> str | None:
    """The persisted ``capacity_evidence`` value for a profile.

    A profile that already carries an evidence string is passed through; one
    that only carries resolved window fields gets a record built from them, so
    the runtime profile and the immutable version agree.
    """
    existing = getattr(profile, "capacity_evidence", None)
    if existing is not None:
        return existing if isinstance(existing, str) else dumps_capacity_record(existing)
    mode = getattr(profile, "working_window_mode", None)
    if mode is None:
        return None
    return dumps_capacity_record({
        "schema": "capacity-evidence-v1",
        "mode": mode,
        "status": getattr(profile, "capacity_status", None) or CAPACITY_STATUS_MANUAL,
        "source": getattr(profile, "capacity_source", None) or "profile",
        "effective_context_limit": getattr(profile, "context_window", None),
        "model_context_limit": getattr(profile, "model_context_limit", None),
        "model_max_output_limit": getattr(profile, "model_max_output_limit", None),
        "admitted_context_limit": getattr(profile, "admitted_context_limit", None),
        "soft_context_limit": getattr(profile, "soft_context_limit", None),
        "counter_id": getattr(profile, "counter_id", None),
        "counter_version": getattr(profile, "counter_version", None),
        "counter_mode": getattr(profile, "counter_mode", None),
        "evidence_refs": [],
        "catalog_version": None,
        "verified_at": None,
        "reason": "",
    })


def loads_capacity_record(value: Any) -> dict[str, Any] | None:
    """Read a capacity record from a persisted ``capacity_evidence`` value.

    Legacy rows store a plain text evidence note; those are returned as ``None``
    (unknown metadata) rather than being mistaken for a resolved capacity.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Catalog (T03). Every entry must trace back to an official snapshot.
# ---------------------------------------------------------------------------

_DEEPSEEK_EVIDENCE = CapacityEvidence(
    urls=(
        "https://api-docs.deepseek.com/quick_start/pricing",
        "https://api-docs.deepseek.com/api/create-chat-completion",
        "https://api-docs.deepseek.com/quick_start/token_usage",
    ),
    fetched_at="2026-09-19T11:07:32Z",
    snapshot_sha256=(
        "2FECEE48BF6AD791BCE38D1D5504D8AD5C8B0FD4DA93E6DC198AE88FF1A4506A",
        "4C2384D7AD4E5F1929EDB922900B2A96546A7E699E8B067D6E82DCB9A330989D",
        "4922DA4DD1A67DEEBB3FBD4A0A5F0D4E109B04874D4167CCC61766EDE2FCC4A7",
    ),
    notes=(
        "Official pages state context length '1M' without an exact integer and "
        "max output '384K (393216)'. The exact context integer stays unresolved."
    ),
)

DEEPSEEK_FLASH = CapacityEntry(
    provider="deepseek",
    base_url="https://api.deepseek.com",
    protocol="openai_compatible",
    model_id="deepseek-flash",
    model_version="DeepSeek-V4.1-Flash",
    aliases=("deepseek-v4-flash", "deepseek-v4-flash-vision-exp"),
    context_limit=None,  # unresolved: official page only says 1M
    default_context_limit=1_000_000,  # adopted conservative integer for "1M"
    max_output_limit=393216,  # 384K, verified
    counter_id="deepseek-text-estimate",
    counter_version="deepseek-text-estimate-v1",
    counter_verified=False,
    context_verified=False,
    verified_at="2026-09-19T11:07:32Z",
    evidence=_DEEPSEEK_EVIDENCE,
)

DEEPSEEK_FLASH_ANTHROPIC = replace(
    DEEPSEEK_FLASH,
    protocol="anthropic",
    base_url="https://api.deepseek.com/anthropic",
)

CATALOG: tuple[CapacityEntry, ...] = (
    DEEPSEEK_FLASH,
    DEEPSEEK_FLASH_ANTHROPIC,
)
