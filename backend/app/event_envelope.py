"""Versioned event metadata layered over existing immutable domain events."""
from __future__ import annotations

from dataclasses import dataclass
import json

from .execution_context import HarnessExecutionContext, serialize_context, deserialize_context
from .execution_outcome import ExecutionOutcome


@dataclass(frozen=True)
class EventMetadata:
    context: HarnessExecutionContext
    source: str
    operation_id: str
    attempt: int = 1
    causation_event_id: str | None = None
    outcome: ExecutionOutcome | None = None
    snapshot_refs: tuple[str, ...] = ()
    payload_version: int = 1

    def __post_init__(self):
        if not isinstance(self.context, HarnessExecutionContext):
            raise ValueError("trusted execution context required")
        for value in (self.source, self.operation_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("event source and operation must be nonempty")
        if type(self.attempt) is not int or self.attempt < 1 or type(self.payload_version) is not int or self.payload_version != 1:
            raise ValueError("invalid event version or attempt")
        if self.causation_event_id is not None and (not isinstance(self.causation_event_id, str) or not self.causation_event_id):
            raise ValueError("empty causation reference")
        if isinstance(self.snapshot_refs, str):
            raise ValueError("snapshot_refs must be a sequence of references")
        object.__setattr__(self, "snapshot_refs", tuple(self.snapshot_refs))
        if any(not isinstance(ref, str) or not ref for ref in self.snapshot_refs):
            raise ValueError("invalid snapshot reference")
        if self.outcome is not None and not isinstance(self.outcome, ExecutionOutcome):
            raise ValueError("invalid event outcome")

    def to_dict(self):
        return {"schema_version": 1, "context": json.loads(serialize_context(self.context)),
                "source": self.source, "operation_id": self.operation_id, "attempt": self.attempt,
                "causation_event_id": self.causation_event_id, "payload_version": self.payload_version,
                "outcome": self.outcome.to_dict() if self.outcome else None,
                "snapshot_refs": list(self.snapshot_refs)}

    @classmethod
    def from_dict(cls, value):
        body = dict(value)
        version = body.pop("schema_version", None)
        if type(version) is not int or version != 1:
            raise ValueError("unsupported event envelope version")
        body["context"] = deserialize_context(body["context"])
        if body.get("outcome") is not None:
            body["outcome"] = ExecutionOutcome.from_dict(body["outcome"])
        return cls(**body)

