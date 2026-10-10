"""Serializable execution semantics. No I/O, business services or retry policy."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


class Scope(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    LOOP = "loop"
    TASK = "task"


class Status(StrEnum):
    COMPLETED = "completed"
    AWAITING_INPUT = "awaiting_input"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_EXTERNAL = "awaiting_external"
    BLOCKED = "blocked"
    HANDOFF = "handoff"
    CANCELLED = "cancelled"
    EXHAUSTED = "exhausted"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class Effect(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    NOT_STARTED = "not_started"
    APPLIED = "applied"
    UNKNOWN = "unknown"


class Phase(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    AUTHORIZATION = "authorization"
    LOOP = "loop"
    TASK = "task"


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True)
class ExecutionError:
    code: str
    phase: Phase
    retryable: bool
    public_message: str
    diagnostic_ref: str | None = None
    attempts: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.code):
            raise ValueError("invalid error code")
        object.__setattr__(self, "phase", Phase(self.phase))
        if type(self.retryable) is not bool or type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("invalid retry metadata")
        _identifier(self.public_message, "public_message")
        if self.diagnostic_ref is not None:
            _identifier(self.diagnostic_ref, "diagnostic_ref")

    def to_dict(self) -> dict:
        return {"code": self.code, "phase": self.phase.value, "retryable": self.retryable,
                "public_message": self.public_message, "diagnostic_ref": self.diagnostic_ref,
                "attempts": self.attempts}


_WAITING = {Status.AWAITING_INPUT, Status.AWAITING_APPROVAL, Status.AWAITING_EXTERNAL}


@dataclass(frozen=True)
class ExecutionOutcome:
    """A validated discriminated record; references contain no domain payload.

    Cross-field validation keeps illegal states out without a subclass for
    every status. All fields are immutable scalars or immutable value objects.
    """

    scope: Scope
    status: Status
    error: ExecutionError | None = None
    reason: str | None = None
    continuation_ref: str | None = None
    target_ref: str | None = None
    artifact_ref: str | None = None
    effect: Effect = Effect.NOT_APPLICABLE
    reconciliation_ref: str | None = None

    def __post_init__(self) -> None:
        for name, enum in (("scope", Scope), ("status", Status), ("effect", Effect)):
            object.__setattr__(self, name, enum(getattr(self, name)))
        for name in ("reason", "continuation_ref", "target_ref", "artifact_ref", "reconciliation_ref"):
            if getattr(self, name) is not None:
                _identifier(getattr(self, name), name)
        if self.error is not None and not isinstance(self.error, ExecutionError):
            raise ValueError("error must be an ExecutionError")
        if (self.status in {Status.FAILED, Status.RECONCILIATION_REQUIRED}) != (self.error is not None):
            raise ValueError("only failed/reconciliation results carry an error")
        if (self.status in _WAITING) != (self.continuation_ref is not None):
            raise ValueError("waiting requires a continuation reference")
        if (self.status == Status.HANDOFF) != (self.target_ref is not None):
            raise ValueError("handoff requires a target reference")
        if self.status in {Status.EXHAUSTED, Status.CANCELLED, Status.BLOCKED} and self.reason is None:
            raise ValueError("stopped execution requires a reason")
        if self.artifact_ref is not None and self.status != Status.COMPLETED:
            raise ValueError("only completed results carry an artifact")
        if (self.effect == Effect.UNKNOWN) != (self.reconciliation_ref is not None):
            raise ValueError("unknown effects require a reconciliation reference")
        if self.status == Status.RECONCILIATION_REQUIRED and self.effect != Effect.UNKNOWN:
            raise ValueError("reconciliation requires an unknown effect")
        if self.effect == Effect.UNKNOWN and self.status not in {Status.RECONCILIATION_REQUIRED, Status.CANCELLED}:
            raise ValueError("unknown effects must stop for reconciliation")

    def to_dict(self) -> dict:
        return {"schema_version": 1, "scope": self.scope.value, "status": self.status.value,
                "error": self.error.to_dict() if self.error else None, "reason": self.reason,
                "continuation_ref": self.continuation_ref, "target_ref": self.target_ref,
                "artifact_ref": self.artifact_ref, "effect": self.effect.value,
                "reconciliation_ref": self.reconciliation_ref}

    @classmethod
    def from_dict(cls, value: dict) -> ExecutionOutcome:
        body = dict(value)
        version = body.pop("schema_version", None)
        if type(version) is not int or version != 1:
            raise ValueError("unsupported outcome schema version")
        if body.get("error") is not None:
            body["error"] = ExecutionError(**body["error"])
        return cls(**body)
