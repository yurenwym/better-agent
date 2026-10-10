"""Authorized read projections for versioned and historical domain events."""
import json

from .event_envelope import EventMetadata


def project_envelope(event, *, owner_id: str) -> dict:
    """Authorized, redacted read projection; never use this to resume work.

    Old records have no provable owner in this layer and must be read through
    the existing authorized domain endpoint, not this trace projection.
    """
    from .events import _redact_value
    if not event.envelope_json:
        raise PermissionError("legacy event requires domain authorization")
    metadata = EventMetadata.from_dict(json.loads(event.envelope_json))
    if metadata.context.owner_id != owner_id:
        raise PermissionError("event owner mismatch")
    stream_kind = "thread" if hasattr(event, "thread_id") else "run"
    stream_id = event.thread_id if stream_kind == "thread" else event.run_id
    return {**metadata.to_dict(), "event_id": event.event_id, "type": event.type,
            "occurred_at": event.occurred_at, "actor": event.actor,
            "stream_kind": stream_kind, "stream_id": stream_id, "seq": event.seq,
            "payload": _redact_value(event.data, None, "data")}


def project_authorized_event(event, *, owner_id: str) -> dict:
    """Read a mixed-version stream AFTER its domain has authorized the owner.

    Unknown versions are represented by a cursor and compatibility status;
    their unchecked identity/payload is never exposed by this projection.
    """
    if not event.envelope_json:
        return {"event_id": event.event_id, "seq": event.seq, "type": event.type,
                "compatibility": "legacy_context_missing"}
    body = json.loads(event.envelope_json)
    if type(body.get("schema_version")) is not int or body["schema_version"] != 1:
        return {"event_id": event.event_id, "seq": event.seq, "type": event.type,
                "compatibility": "unsupported_version"}
    return project_envelope(event, owner_id=owner_id)
