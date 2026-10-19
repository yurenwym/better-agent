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


def _compatibility(event, status: str) -> dict:
    """A bad row keeps its stored time and id and carries no envelope body."""
    return {
        "event_id": event.event_id, "seq": event.seq, "type": event.type,
        "occurred_at": event.occurred_at, "compatibility": status,
    }


def project_authorized_event(event, *, owner_id: str) -> dict:
    """Read a mixed-version stream AFTER its domain has authorized the owner.

    A row whose envelope is an array, null, missing fields, a wrong nested
    type, or an invalid field becomes a compatibility marker.  One such row
    does not reject its neighbours.  An owner mismatch is still refused.
    """
    if not event.envelope_json:
        return _compatibility(event, "legacy_context_missing")
    try:
        body = json.loads(event.envelope_json)
    except (TypeError, ValueError):
        return _compatibility(event, "malformed_envelope")
    if not isinstance(body, dict):
        return _compatibility(event, "malformed_envelope")
    version = body.get("schema_version")
    if type(version) is not int or version != 1:
        return _compatibility(event, "unsupported_version")
    try:
        return project_envelope(event, owner_id=owner_id)
    except PermissionError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        return _compatibility(event, "malformed_envelope")
