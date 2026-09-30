"""Trusted, immutable execution context shared by model and tool calls.

The harness - the conversation worker and the approval-resume path - is the only
place that mints identity.  Everything downstream converts an existing context
through ``ModelCallContext.from_harness`` / ``ToolExecutionContext.from_harness``
and must not invent an owner, a budget root, a bundle or a trace of its own.

A span identifies one *logical* call, not a process and not a wall-clock
interval: an approval that waits for a human keeps its original tool span, and
the next internal model call gets a child span.

This module is deliberately free of database and service imports.  Persistence
lives in :mod:`app.harness_context_store`.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

#: Version of the persisted envelope.  A stored context whose version is not
#: recognised is rejected instead of being coerced into the current shape.
SCHEMA_VERSION = "harness-execution-context-v1"

#: Server-generated identifiers.  ``uuid4().hex`` is what every factory here
#: produces, so anything else is a malformed context rather than a style choice.
_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

#: Every field of the context, in the order they are serialised.  All optional
#: fields are written explicitly as ``null`` so the envelope has one shape.
CONTEXT_FIELDS: tuple[str, ...] = (
    "owner_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "thread_id",
    "turn_id",
    "run_id",
    "task_id",
    "parent_task_id",
    "root_task_id",
    "root_budget_id",
    "runtime_bundle_id",
    "project_id",
)

#: Legacy context field -> harness field.  A context type that keeps both its
#: own fields and the harness must never carry two disagreeing identities, so
#: every pair here is checked whenever both are readable.
HARNESS_FIELD_FOR_LEGACY: Mapping[str, str] = {
    "owner_id": "owner_id",
    "run_id": "run_id",
    "thread_id": "thread_id",
    "turn_id": "turn_id",
    "project_id": "project_id",
    "agent_task_id": "task_id",
    "root_budget_id": "root_budget_id",
    "runtime_bundle_id": "runtime_bundle_id",
    **{name: name for name in ("trace_id", "span_id", "parent_span_id",
                              "task_id", "parent_task_id", "root_task_id")},
}


class HarnessContextError(ValueError):
    """A context was rejected.  Never silently repaired."""

    code = "HARNESS_CONTEXT_INVALID"


class UnknownSchemaVersion(HarnessContextError):
    code = "UNKNOWN_SCHEMA_VERSION"


class ContextIdentityConflict(HarnessContextError):
    code = "CONTEXT_IDENTITY_CONFLICT"


class LegacyContextMissing(HarnessContextError):
    """A record written before this phase has no context to resume from."""

    code = "LEGACY_CONTEXT_MISSING"


def _require_identifier(value: Any, name: str) -> None:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise HarnessContextError(f"{name} must be a server-generated uuid hex")


def _require_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise HarnessContextError(f"{name} must be a non-empty string")


def validate_context(context: "HarnessExecutionContext") -> None:
    """Field and inheritance invariants.  Raises instead of normalising."""
    _require_text(context.owner_id, "owner_id")
    _require_identifier(context.trace_id, "trace_id")
    _require_identifier(context.span_id, "span_id")
    if context.parent_span_id is not None:
        _require_identifier(context.parent_span_id, "parent_span_id")
        if context.parent_span_id == context.span_id:
            raise HarnessContextError("a span must not be its own parent")
    for name in (
        "thread_id", "turn_id", "run_id", "task_id", "parent_task_id",
        "root_task_id", "root_budget_id", "runtime_bundle_id", "project_id",
    ):
        value = getattr(context, name)
        if value is not None:
            _require_text(value, name)
    if context.parent_span_id is None:
        # A root span has no parent task and is the root of its own task tree.
        if context.parent_task_id is not None:
            raise HarnessContextError("a root span must not have a parent task")
        if context.root_task_id != context.task_id:
            raise HarnessContextError("root_task_id must equal the root task_id")
    elif context.parent_task_id is not None and context.task_id is None:
        raise HarnessContextError("a child span without a task must not carry a parent task")
    if (context.task_id is None) != (context.root_task_id is None):
        raise HarnessContextError("task_id and root_task_id must both be set or both be empty")


def check_context_alignment(context: Any) -> None:
    """Reject a legacy field that contradicts the harness it was derived from.

    None is an identity value too; adapters cannot fill or change it.
    """
    harness = getattr(context, "harness", None)
    if harness is None:
        return
    if not isinstance(harness, HarnessExecutionContext):
        raise ContextIdentityConflict("harness must be a HarnessExecutionContext")
    for legacy, harness_field in HARNESS_FIELD_FOR_LEGACY.items():
        if not hasattr(context, legacy):
            continue
        expected = getattr(harness, harness_field)
        actual = getattr(context, legacy)
        if expected != actual:
            raise ContextIdentityConflict(
                f"{legacy} contradicts the harness it was built from"
            )


def rebind(context: Any, **updates: Any) -> Any:
    """Update routing metadata, preserving every harness identity field.

    Legacy callers without a harness retain their existing binding behavior.
    A harness has already been bound at its trusted root, including nulls.
    """
    check_context_alignment(context)
    harness = getattr(context, "harness", None)
    if harness is not None:
        if "harness" in updates and updates["harness"] != harness:
            raise ContextIdentityConflict("cannot replace a bound harness")
        for name, value in updates.items():
            field = HARNESS_FIELD_FOR_LEGACY.get(name)
            if field and value != getattr(harness, field):
                raise ContextIdentityConflict(f"cannot rebind harness field {name}")
    return replace(context, **updates)


@dataclass(frozen=True)
class HarnessExecutionContext:
    """Immutable identity of one logical call.

    ``owner_id`` is required and comes from a verified server-side identity; a
    missing owner is an error, never a fallback to ``local-user``.  A dataclass
    is not a security sandbox: the factories, the call boundaries and the tests
    are what keep production code from assembling an identity by hand.
    """

    owner_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    root_task_id: str | None = None
    root_budget_id: str | None = None
    runtime_bundle_id: str | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        validate_context(self)

    @property
    def is_root_span(self) -> bool:
        return self.parent_span_id is None

    def public_view(self) -> dict[str, Any]:
        """The context as it may appear in an API response or an event body."""
        return {name: getattr(self, name) for name in CONTEXT_FIELDS}

    def child(self) -> "HarnessExecutionContext":
        return create_child_context(self)


def create_root_context(
    *,
    owner_id: str,
    thread_id: str | None = None,
    turn_id: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    root_budget_id: str | None = None,
    runtime_bundle_id: str | None = None,
) -> HarnessExecutionContext:
    """Mint a trace at a trusted entry point.

    Keyword-only and closed over its parameters on purpose: there is no
    ``**overrides`` and no way to hand a caller-supplied trace or owner.
    """
    return HarnessExecutionContext(
        owner_id=owner_id,
        trace_id=uuid.uuid4().hex,
        span_id=uuid.uuid4().hex,
        parent_span_id=None,
        thread_id=thread_id,
        turn_id=turn_id,
        run_id=run_id,
        task_id=task_id,
        parent_task_id=None,
        root_task_id=task_id,
        root_budget_id=root_budget_id,
        runtime_bundle_id=runtime_bundle_id,
        project_id=project_id,
    )


def create_child_context(parent: HarnessExecutionContext) -> HarnessExecutionContext:
    """Derive a new span.  Business identity and the trace are inherited."""
    if not isinstance(parent, HarnessExecutionContext):
        raise HarnessContextError("parent must be a HarnessExecutionContext")
    return replace(parent, span_id=uuid.uuid4().hex, parent_span_id=parent.span_id)


def context_envelope(context: HarnessExecutionContext) -> dict[str, Any]:
    if not isinstance(context, HarnessExecutionContext):
        raise HarnessContextError("context must be a HarnessExecutionContext")
    return {"schema_version": SCHEMA_VERSION, "context": context.public_view()}


def canonical_json(envelope: Mapping[str, Any]) -> str:
    """The one serialisation the digest is defined over."""
    return json.dumps(
        envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )


def serialize_context(context: HarnessExecutionContext) -> str:
    return canonical_json(context_envelope(context))


def deserialize_context(payload: str | bytes | Mapping[str, Any]) -> HarnessExecutionContext:
    """Rebuild a context from a stored envelope, rejecting anything unknown.

    Unknown schema versions, unknown fields and missing fields are all refused;
    the envelope never becomes a way to inject identity the harness did not mint.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        try:
            raw: Any = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise HarnessContextError("execution context is not valid JSON") from exc
    else:
        raw = payload
    if not isinstance(raw, Mapping):
        raise HarnessContextError("execution context must be an object")
    if set(raw) != {"schema_version", "context"}:
        raise HarnessContextError("execution context envelope has unexpected keys")
    version = raw["schema_version"]
    if version != SCHEMA_VERSION:
        raise UnknownSchemaVersion(f"unsupported execution context schema: {version!r}")
    body = raw["context"]
    if not isinstance(body, Mapping):
        raise HarnessContextError("execution context body must be an object")
    unknown = set(body) - set(CONTEXT_FIELDS)
    if unknown:
        raise HarnessContextError(
            "execution context carries unknown fields: " + ",".join(sorted(unknown))
        )
    missing = set(CONTEXT_FIELDS) - set(body)
    if missing:
        raise HarnessContextError(
            "execution context is missing fields: " + ",".join(sorted(missing))
        )
    return HarnessExecutionContext(**{name: body[name] for name in CONTEXT_FIELDS})


def execution_context_digest(payload: Any) -> str:
    """Digest of a context or a stored envelope.

    The payload is re-parsed and re-serialised canonically first, so whitespace
    and key order cannot change the digest while any field value does.
    """
    context = payload if isinstance(payload, HarnessExecutionContext) else deserialize_context(payload)
    return hashlib.sha256(canonical_json(context_envelope(context)).encode("utf-8")).hexdigest()
