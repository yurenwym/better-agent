"""The frozen logical input of one logical LLM call.

Two contexts, two questions:

* :class:`~app.execution_context.HarnessExecutionContext` answers *who* called,
  under which task, budget and runtime version.
* :class:`ModelInputSnapshot` answers *what was actually provided* to one
  logical call.

A snapshot is created once, before anything is admitted, counted or sent, and it
never changes afterwards.  The authoritative content is the canonical JSON
string ``content_json``: every accessor re-parses it, so a snapshot can never
carry two disagreeing copies of the same input and ``to_request()`` always hands
out a private copy.

The snapshot is *provider independent*: profile defaults, protocol field naming
and the real HTTP body belong to the attempt layer and are derived from this
content, never written back into it.

This module is deliberately free of database and service imports.  Persistence
lives in :mod:`app.model_input_snapshot_store`.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: Version of the persisted envelope.  A stored snapshot whose version is not
#: recognised is rejected instead of being coerced into the current shape.
SCHEMA_VERSION = "model-input-snapshot-v1"

_ENVELOPE_KEYS = frozenset({"schema_version", "binding", "request", "provenance"})
_BINDING_FIELDS: tuple[str, ...] = ("owner_id", "runtime_bundle_id", "role", "purpose")
_REQUEST_FIELDS: tuple[str, ...] = (
    "messages", "tools", "temperature", "max_tokens", "thinking", "response_format", "single_attempt",
)
_SOURCE_FIELDS: tuple[str, ...] = (
    "kind", "id", "version", "content_digest", "included", "location", "dropped_reason",
)
_PROVENANCE_FIELDS: tuple[str, ...] = ("status", "sources", "assembly")

PROVENANCE_COMPLETE = "complete"
PROVENANCE_PARTIAL = "partial"
PROVENANCE_STATUSES: tuple[str, ...] = (PROVENANCE_COMPLETE, PROVENANCE_PARTIAL)

#: Kinds this phase knows how to name.  Anything else is recorded as ``other``
#: rather than being dropped, so an unrecognised source is still visible.
SOURCE_KINDS: tuple[str, ...] = (
    "bundle", "prompt", "memory", "revision", "skill", "tool", "attachment", "history", "summary", "other",
)


class SnapshotError(ValueError):
    """A snapshot was rejected.  Never silently repaired."""

    code = "SNAPSHOT_INVALID"


class UnknownSnapshotVersion(SnapshotError):
    code = "UNKNOWN_SNAPSHOT_VERSION"


class SnapshotIntegrityError(SnapshotError):
    """Stored content and its digest, or two copies of the same field, disagree."""

    code = "SNAPSHOT_INTEGRITY_ERROR"


class SnapshotBindingConflict(SnapshotError):
    """The envelope's binding disagrees with the row or the invocation it belongs to."""

    code = "SNAPSHOT_BINDING_CONFLICT"


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise SnapshotError(f"{name} must be a non-empty string")


def _require_optional_text(value: Any, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        raise SnapshotError(f"{name} must be a non-empty string or null")


def json_value(value: Any, path: str) -> Any:
    """Return a JSON-safe copy of ``value`` or refuse it.

    Objects are never coerced through ``str()``: an unsupported type is a bug in
    the caller, not something to render into the digest.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SnapshotError(f"{path} must be a finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SnapshotError(f"{path} keys must be strings")
            result[key] = json_value(item, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise SnapshotError(f"{path} carries an unsupported value of type {type(value).__name__}")


@dataclass(frozen=True)
class SnapshotSource:
    """One input source as it stood when the input was frozen.

    ``included`` is a statement about the *final* input: content that was
    assembled and then dropped must be recorded with ``included=False`` rather
    than omitted, so the snapshot never claims a source it did not send.
    """

    kind: str
    id: str
    version: str | None = None
    content_digest: str | None = None
    included: bool = True
    location: dict[str, Any] | None = None
    dropped_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in SOURCE_KINDS:
            raise SnapshotError(f"unsupported source kind: {self.kind!r}")
        _require_text(self.id, "source.id")
        _require_optional_text(self.version, "source.version")
        _require_optional_text(self.content_digest, "source.content_digest")
        _require_optional_text(self.dropped_reason, "source.dropped_reason")
        if not isinstance(self.included, bool):
            raise SnapshotError("source.included must be a boolean")
        if self.location is not None:
            json_value(self.location, "source.location")

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "version": self.version,
            "content_digest": self.content_digest,
            "included": self.included,
            "location": json_value(self.location, "source.location") if self.location is not None else None,
            "dropped_reason": self.dropped_reason,
        }

    @property
    def fully_tracked(self) -> bool:
        """Whether this source can be accounted for end to end."""
        if self.included:
            return self.location is not None
        return self.dropped_reason is not None


@dataclass(frozen=True)
class SnapshotProvenance:
    """Where the frozen input came from, and how completely that is known.

    ``complete`` is a claim: every source is either located in the final input
    or carries a reason it was dropped.  A source that was merely *found* in the
    assembled text (a substring hit) does not qualify, which is why phase 2A
    records ``partial``.
    """

    status: str = PROVENANCE_PARTIAL
    sources: tuple[SnapshotSource, ...] = ()
    assembly: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in PROVENANCE_STATUSES:
            raise SnapshotError(f"unsupported provenance status: {self.status!r}")
        coerced = tuple(self.sources)
        for source in coerced:
            if not isinstance(source, SnapshotSource):
                raise SnapshotError("provenance sources must be SnapshotSource values")
        object.__setattr__(self, "sources", coerced)
        if self.assembly is not None:
            json_value(self.assembly, "provenance.assembly")
        if self.status == PROVENANCE_COMPLETE:
            if not coerced:
                raise SnapshotError("provenance cannot be complete without any source")
            untracked = [source.id for source in coerced if not source.fully_tracked]
            if untracked:
                raise SnapshotError(
                    "provenance cannot be complete while sources are untracked: "
                    + ",".join(sorted(untracked))
                )

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sources": [source.payload() for source in self.sources],
            "assembly": json_value(self.assembly, "provenance.assembly") if self.assembly is not None else None,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SnapshotProvenance":
        raw = _require_mapping(payload, "provenance")
        _require_exact_keys(raw, _PROVENANCE_FIELDS, "provenance")
        sources = raw["sources"]
        if not isinstance(sources, (list, tuple)):
            raise SnapshotError("provenance.sources must be a list")
        return cls(
            status=raw["status"],
            sources=tuple(_source_from_payload(item) for item in sources),
            assembly=raw["assembly"],
        )


def _source_from_payload(payload: Any) -> SnapshotSource:
    raw = _require_mapping(payload, "source")
    _require_exact_keys(raw, _SOURCE_FIELDS, "source")
    return SnapshotSource(
        kind=raw["kind"],
        id=raw["id"],
        version=raw["version"],
        content_digest=raw["content_digest"],
        included=raw["included"],
        location=raw["location"],
        dropped_reason=raw["dropped_reason"],
    )


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotError(f"{name} must be an object")
    return value


def _require_exact_keys(payload: Mapping[str, Any], expected: Sequence[str], name: str) -> None:
    unknown = set(payload) - set(expected)
    if unknown:
        raise SnapshotError(f"{name} carries unknown fields: " + ",".join(sorted(unknown)))
    missing = set(expected) - set(payload)
    if missing:
        raise SnapshotError(f"{name} is missing fields: " + ",".join(sorted(missing)))


def canonical_json(envelope: Mapping[str, Any]) -> str:
    """The one serialisation the digest is defined over."""
    return json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def deserialize_envelope(payload: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Parse and validate a stored envelope, refusing anything unknown.

    Array order is preserved; key order is not significant.  Unknown schema
    versions, unknown or missing fields and unsupported value types are all
    rejected rather than coerced.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        try:
            raw: Any = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise SnapshotError("model input snapshot is not valid JSON") from exc
    else:
        raw = payload
    envelope = dict(_require_mapping(raw, "snapshot"))
    # The version decides which shape is legal, so it is read before the shape is
    # checked: a v99 envelope must be refused as an unknown version, not as a v1
    # envelope that happens to be missing v1's fields.
    if "schema_version" not in envelope:
        raise SnapshotError("snapshot is missing fields: schema_version")
    version = envelope["schema_version"]
    if version != SCHEMA_VERSION:
        raise UnknownSnapshotVersion(f"unsupported model input snapshot schema: {version!r}")
    _require_exact_keys(envelope, _ENVELOPE_KEYS, "snapshot")
    binding = dict(_require_mapping(envelope["binding"], "binding"))
    _require_exact_keys(binding, _BINDING_FIELDS, "binding")
    _require_text(binding["owner_id"], "binding.owner_id")
    _require_text(binding["role"], "binding.role")
    _require_text(binding["purpose"], "binding.purpose")
    _require_optional_text(binding["runtime_bundle_id"], "binding.runtime_bundle_id")
    request = dict(_require_mapping(envelope["request"], "request"))
    _require_exact_keys(request, _REQUEST_FIELDS, "request")
    messages = request["messages"]
    if not isinstance(messages, list):
        raise SnapshotError("request.messages must be a list")
    request["messages"] = json_value(messages, "request.messages")
    if request["tools"] is not None:
        if not isinstance(request["tools"], list):
            raise SnapshotError("request.tools must be a list or null")
        request["tools"] = json_value(request["tools"], "request.tools")
    request["temperature"] = json_value(request["temperature"], "request.temperature")
    request["max_tokens"] = json_value(request["max_tokens"], "request.max_tokens")
    request["thinking"] = json_value(request["thinking"], "request.thinking")
    if request["response_format"] is not None:
        request["response_format"] = json_value(request["response_format"], "request.response_format")
    if not isinstance(request["single_attempt"], bool):
        raise SnapshotError("request.single_attempt must be a boolean")
    provenance = SnapshotProvenance.from_payload(envelope["provenance"])
    return {
        "schema_version": version,
        "binding": binding,
        "request": request,
        "provenance": provenance.payload(),
    }


def snapshot_digest(payload: Any) -> str:
    """Digest over the canonical envelope.

    The payload is re-validated and re-serialised first, so whitespace and key
    order cannot change the digest while any field value does.
    """
    if isinstance(payload, ModelInputSnapshot):
        payload = payload.content_json
    envelope = deserialize_envelope(payload)
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ModelInputSnapshot:
    """Immutable logical input of one logical LLM call.

    ``content_json`` is authoritative.  ``id`` is only set once the snapshot has
    been persisted, and is deliberately *not* part of the envelope: two
    invocations that send identical content share a digest but still get
    independent rows.
    """

    owner_id: str
    role: str
    purpose: str
    runtime_bundle_id: str | None
    content_json: str
    content_digest: str
    schema_version: str = SCHEMA_VERSION
    id: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise UnknownSnapshotVersion(f"unsupported model input snapshot schema: {self.schema_version!r}")
        _require_text(self.owner_id, "owner_id")
        _require_text(self.role, "role")
        _require_text(self.purpose, "purpose")
        _require_optional_text(self.runtime_bundle_id, "runtime_bundle_id")
        _require_optional_text(self.id, "id")
        if not isinstance(self.content_json, str) or not self.content_json:
            raise SnapshotError("content_json must be a non-empty string")
        envelope = deserialize_envelope(self.content_json)
        binding = envelope["binding"]
        for name in _BINDING_FIELDS:
            if binding[name] != getattr(self, name):
                raise SnapshotBindingConflict(
                    f"snapshot binding.{name} contradicts the snapshot's own field"
                )
        if snapshot_digest(self.content_json) != self.content_digest:
            raise SnapshotIntegrityError("snapshot digest does not match its content")

    # ---------------------------------------------------------------- views

    def envelope(self) -> dict[str, Any]:
        """A fresh copy of the whole envelope."""
        return deserialize_envelope(self.content_json)

    def request_payload(self) -> dict[str, Any]:
        """A fresh copy of the frozen request block."""
        return self.envelope()["request"]

    def provenance(self) -> SnapshotProvenance:
        return SnapshotProvenance.from_payload(self.envelope()["provenance"])

    def to_request(self) -> Any:
        """A brand-new :class:`ModelRequest` built from the frozen content.

        Re-parsing is what makes the copy independent: no message, nested tool
        schema or response format is shared with the snapshot or with another
        copy handed out earlier.
        """
        from .model_gateway import ModelRequest

        request = self.request_payload()
        return ModelRequest(
            messages=request["messages"],
            tools=request["tools"],
            temperature=request["temperature"],
            max_tokens=request["max_tokens"],
            role=self.role,
            purpose=self.purpose,
            thinking=request["thinking"],
            response_format=request["response_format"],
            single_attempt=request["single_attempt"],
        )

    def public_view(self) -> dict[str, Any]:
        """Reference-level view: ids, digests and counts, never user text."""
        envelope = self.envelope()
        request = envelope["request"]
        provenance = envelope["provenance"]
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "owner_id": self.owner_id,
            "runtime_bundle_id": self.runtime_bundle_id,
            "role": self.role,
            "purpose": self.purpose,
            "content_digest": self.content_digest,
            "message_count": len(request["messages"]),
            "tool_count": None if request["tools"] is None else len(request["tools"]),
            "provenance_status": provenance["status"],
            "source_count": len(provenance["sources"]),
        }

    def bound_to(self, owner_id: str, *, runtime_bundle_id: str | None, role: str, purpose: str) -> bool:
        """Whether this snapshot may be reused for the given invocation identity."""
        return (
            self.owner_id == owner_id
            and self.runtime_bundle_id == runtime_bundle_id
            and self.role == role
            and self.purpose == purpose
        )


def freeze_model_input(
    request: Any,
    context: Any,
    provenance: SnapshotProvenance | None = None,
) -> ModelInputSnapshot:
    """Freeze one logical call's input, synchronously and without I/O.

    Called before capacity admission and before any persistence, so that what is
    counted, stored and sent is one and the same content.  ``context`` supplies
    the binding (owner, bundle, role, purpose); a caller cannot pass a binding of
    its own.
    """
    from .model_gateway import ModelRequest

    if not isinstance(request, ModelRequest):
        raise SnapshotError("request must be a ModelRequest")
    owner_id = getattr(context, "owner_id", None)
    role = getattr(context, "role", None)
    purpose = getattr(context, "purpose", None)
    bundle = getattr(context, "runtime_bundle_id", None)
    _require_text(owner_id, "owner_id")
    _require_text(role, "role")
    _require_text(purpose, "purpose")
    _require_optional_text(bundle, "runtime_bundle_id")
    if provenance is not None and not isinstance(provenance, SnapshotProvenance):
        raise SnapshotError("provenance must be a SnapshotProvenance")
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "binding": {
            "owner_id": owner_id,
            "runtime_bundle_id": bundle,
            "role": role,
            "purpose": purpose,
        },
        "request": {
            "messages": json_value(list(request.messages), "request.messages"),
            "tools": None if request.tools is None else json_value(list(request.tools), "request.tools"),
            "temperature": json_value(request.temperature, "request.temperature"),
            "max_tokens": json_value(request.max_tokens, "request.max_tokens"),
            "thinking": json_value(request.thinking, "request.thinking"),
            "response_format": (
                None if request.response_format is None
                else json_value(request.response_format, "request.response_format")
            ),
            "single_attempt": bool(request.single_attempt),
        },
        "provenance": (provenance or SnapshotProvenance()).payload(),
    }
    content_json = canonical_json(envelope)
    return ModelInputSnapshot(
        owner_id=owner_id,
        role=role,
        purpose=purpose,
        runtime_bundle_id=bundle,
        content_json=content_json,
        content_digest=hashlib.sha256(content_json.encode("utf-8")).hexdigest(),
    )


def source_from_asset(asset: Mapping[str, Any]) -> SnapshotSource:
    """Map one Learning asset reference onto a snapshot source.

    The asset row already records what was selected and whether it reached the
    text.  That is a *reference* record, not proof of completeness, so a
    provenance built from these stays ``partial`` unless the caller says
    otherwise.
    """
    raw = _require_mapping(asset, "asset")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        kind = "other"
    digest = raw.get("content_hash") or raw.get("package_digest")
    return SnapshotSource(
        kind=kind,
        id=str(raw.get("id") or ""),
        version=raw.get("version") if isinstance(raw.get("version"), str) else None,
        content_digest=digest if isinstance(digest, str) and digest else None,
        included=bool(raw.get("included", False)),
        location=None,
        dropped_reason=None if raw.get("included") else "not_included_in_final_input",
    )


def provenance_from_assets(assets: Sequence[Mapping[str, Any]] | None) -> SnapshotProvenance:
    """A ``partial`` provenance from the Learning asset references of this call."""
    sources = tuple(source_from_asset(asset) for asset in (assets or ()))
    return SnapshotProvenance(status=PROVENANCE_PARTIAL, sources=sources)


def _coerce_source(source: Any) -> SnapshotSource:
    if isinstance(source, SnapshotSource):
        return source
    if isinstance(source, Mapping):
        raw = dict(source)
        # An unrecognised kind is still recorded, as ``other``: dropping it
        # would make the input look better sourced than it is.
        if raw.get("kind") not in SOURCE_KINDS:
            raw["kind"] = "other"
        values = {field: raw.get(field) for field in _SOURCE_FIELDS}
        if "included" not in raw or not isinstance(raw.get("included"), bool):
            values["included"] = True
        return SnapshotSource(**values)
    raise SnapshotError(f"unsupported provenance source: {type(source).__name__}")


def build_provenance(
    sources: Sequence[Any] | None = None,
    *,
    status: str = PROVENANCE_PARTIAL,
    assembly: Mapping[str, Any] | None = None,
) -> SnapshotProvenance:
    """Assemble provenance from what the call site actually knows.

    The default is ``partial`` on purpose.  Phase 2A records the references the
    existing chain can name — memory revisions, skill versions, tool schemas —
    but not the per-fragment positions of history, summaries and attachments,
    and not a complete dropped list.  Claiming ``complete`` under those
    conditions would be a lie that later readers would trust.

    Passing ``status="complete"`` is therefore a *caller assertion* and is
    enforced, not trusted: every source must be either located in the final
    input or carry the reason it was dropped, and at least one source must exist.
    An assertion that cannot be backed fails loudly instead of being downgraded.
    """
    coerced = tuple(_coerce_source(source) for source in (sources or ()))
    return SnapshotProvenance(status=status, sources=coerced, assembly=assembly)
