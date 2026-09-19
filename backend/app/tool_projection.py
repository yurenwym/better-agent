"""R2-01: project large tool results in the context, never in storage.

A tool result is authoritative state, and two rules follow from that:

1. The original is persisted in full and is never rewritten or truncated at
   write time. ``turn_asks.answer_json`` keeps it verbatim.
2. What the *context* carries is a mechanical projection: status, every bounded
   authoritative field, a head and tail excerpt of anything oversized, and a
   reference a paged read can resolve back to the original.

The projection is mechanical on purpose. Replacing a tool result with an LLM
paraphrase would let a summary silently contradict the real result - which is
precisely what "do not substitute an unverified summary for the authoritative
result" forbids.

Nothing here invents a limit of its own: the per-result ceiling is derived from
the routed profile's own input budget, so it scales with the model instead of
being a byte constant that drifts away from it.
"""

from __future__ import annotations

import json
from typing import Any

HEAD_RATIO = 0.6
HARD_CEILING_FACTOR = 8


def result_context_bytes(profile: Any) -> int:
    """Per-tool-result context ceiling, derived from the model's input budget."""
    from .token_budget import effective_input_budget, tool_result_budget

    return tool_result_budget(effective_input_budget(profile))


def _slice(encoded: bytes, start: int, end: int) -> str:
    return encoded[max(start, 0):max(end, 0)].decode("utf-8", errors="ignore")


def _project(value: Any, *, limit: int, path: str, reference: dict[str, Any]) -> Any:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= limit:
            return value
        head = int(limit * HEAD_RATIO)
        tail = max(limit - head, 0)
        return {
            "_truncated": True,
            "_total_bytes": len(encoded),
            "_head": _slice(encoded, 0, head),
            "_tail": _slice(encoded, max(len(encoded) - tail, head), len(encoded)),
            "_reference": {**reference, "path": path},
        }
    if isinstance(value, dict):
        return {
            key: _project(item, limit=limit, path=f"{path}.{key}", reference=reference)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _project(item, limit=limit, path=f"{path}[{index}]", reference=reference)
            for index, item in enumerate(value)
        ]
    return value


def project_result_content(content: str, *, limit_bytes: int, reference: dict[str, Any]) -> str:
    """Replace oversized strings in a tool result with a bounded projection.

    Bounded authoritative fields - question ids, selected options, status codes,
    error kinds - are passed through untouched. Only oversized values are
    replaced, and every replacement records its total size and a resolvable
    reference.
    """
    if limit_bytes <= 0:
        raise ValueError("limit_bytes must be positive")

    try:
        payload: Any = json.loads(content)
    except (TypeError, ValueError):
        payload = content

    projected = _project(payload, limit=limit_bytes, path="$", reference=reference)
    rendered = json.dumps(projected, ensure_ascii=False, sort_keys=True)
    encoded = rendered.encode("utf-8")
    ceiling = limit_bytes * HARD_CEILING_FACTOR
    if len(encoded) <= ceiling:
        return rendered

    # Even bounded fields can add up. Fall back to an envelope that records what
    # was dropped instead of dropping it silently.
    envelope = {
        "_truncated": True,
        "_reason": "projected result still exceeds the ceiling",
        "_ceiling_bytes": ceiling,
        "_reference": reference,
        "_dropped_top_level_keys": sorted(projected) if isinstance(projected, dict) else [],
        "_head": _slice(encoded, 0, limit_bytes),
    }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


class ResultScopeError(PermissionError):
    """A tool result was requested outside its owning scope."""


def read_result_page(
    db: Any,
    call_id: str,
    *,
    owner_id: str,
    offset: int = 0,
    limit: int | None = None,
    profile: Any = None,
) -> dict[str, Any]:
    """Read a page of the **original** tool result.

    The read is scope-checked against the persisted ``owner_id`` rather than a
    caller-supplied thread id, and when a ``profile`` is given the page is
    re-validated against that profile's budget so reading detail cannot blow the
    window it was meant to protect.
    """
    if offset < 0:
        raise ValueError("offset must not be negative")

    with db.connection() as connection:
        row = connection.execute(
            "SELECT owner_id, answer_json FROM turn_asks WHERE call_id=?", (call_id,),
        ).fetchone()
    if row is None:
        raise KeyError(call_id)
    if row["owner_id"] is not None and row["owner_id"] != owner_id:
        raise ResultScopeError("tool result does not belong to the requested owner")

    original = (row["answer_json"] or "").encode("utf-8")
    total = len(original)
    page_limit = limit if limit is not None else max(total, 1)
    if page_limit <= 0:
        raise ValueError("limit must be positive")

    page = _slice(original, offset, offset + page_limit)
    next_offset = offset + len(page.encode("utf-8"))
    has_more = next_offset < total

    result = {
        "call_id": call_id,
        "owner_id": row["owner_id"],
        "total_bytes": total,
        "offset": offset,
        "limit": page_limit,
        "page": page,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
    }

    if profile is not None:
        from .token_budget import assert_request_fits

        assert_request_fits(
            [{"role": "tool", "tool_call_id": call_id, "content": page}], None, profile,
        )
    return result
