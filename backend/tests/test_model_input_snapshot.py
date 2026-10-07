"""U01-U06: the frozen logical input of one LLM call.

These are unit tests.  No database and no network: they pin the serialisation,
the digest and the immutability of :mod:`app.model_input_snapshot`.
"""
from __future__ import annotations

import json

import pytest

from app.model_control import ModelCallContext
from app.model_gateway import ModelRequest
from app.model_input_snapshot import (
    SCHEMA_VERSION,
    ModelInputSnapshot,
    SnapshotBindingConflict,
    SnapshotError,
    SnapshotIntegrityError,
    SnapshotProvenance,
    SnapshotSource,
    UnknownSnapshotVersion,
    build_provenance,
    canonical_json,
    deserialize_envelope,
    freeze_model_input,
    snapshot_digest,
)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

_UNSET = object()


def _context(**overrides) -> ModelCallContext:
    values = {
        "role": "conversation",
        "purpose": "route_and_respond",
        "owner_id": "owner-a",
        "runtime_bundle_id": "bundle-1",
    }
    values.update(overrides)
    return ModelCallContext(**values)


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "你是助手。\n只回答中文。"},
        {"role": "user", "content": "第一问"},
    ]


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "get_today_tasks",
                "description": "列出今天的任务",
                "parameters": {
                    "type": "object",
                    "properties": {"date": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": [],
                },
            },
        }
    ]


def _build(
    *,
    messages: list[dict] | None = None,
    tools: object = _UNSET,
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    response_format: dict | None = None,
    single_attempt: bool = False,
) -> ModelRequest:
    return ModelRequest(
        messages=_messages() if messages is None else messages,
        tools=_tools() if tools is _UNSET else tools,
        temperature=temperature,
        max_tokens=max_tokens,
        thinking=thinking,
        response_format=response_format,
        single_attempt=single_attempt,
    )


def _freeze(**overrides) -> ModelInputSnapshot:
    provenance = overrides.pop("provenance", None)
    return freeze_model_input(_build(**overrides), _context(), provenance)


def _digest(**overrides) -> str:
    return _freeze(**overrides).content_digest


# --------------------------------------------------------------------------- #
# U01
# --------------------------------------------------------------------------- #

def test_u01_key_order_does_not_change_the_digest_but_array_order_does() -> None:
    ordered = _build(messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    reordered_keys = _build(
        messages=[{"content": "s", "role": "system"}, {"content": "u", "role": "user"}],
    )
    first = freeze_model_input(ordered, _context())
    second = freeze_model_input(reordered_keys, _context())
    assert first.content_json == second.content_json
    assert first.content_digest == second.content_digest

    swapped = _build(messages=[{"role": "user", "content": "u"}, {"role": "system", "content": "s"}])
    third = freeze_model_input(swapped, _context())
    assert third.content_digest != first.content_digest


# --------------------------------------------------------------------------- #
# U02
# --------------------------------------------------------------------------- #

_MUTATIONS: dict[str, dict] = {
    "messages": {"messages": [{"role": "user", "content": "另一问"}]},
    "tool_schema": {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_today_tasks",
                    "description": "列出今天的任务",
                    "parameters": {
                        "type": "object",
                        "properties": {"date": {"type": "string"}},  # `limit` removed
                        "required": [],
                    },
                },
            }
        ]
    },
    "response_format": {"response_format": {"type": "json_object"}},
    "source_version": {
        "provenance": SnapshotProvenance(
            status="partial",
            sources=(SnapshotSource(kind="skill", id="skill_version_1", version="v2"),),
        )
    },
    "single_attempt": {"single_attempt": True},
}


@pytest.mark.parametrize("mutated", sorted(_MUTATIONS))
def test_u02_one_dimension_changes_its_digest_and_leaves_the_others_alone(mutated: str) -> None:
    baseline = _digest()
    others = [name for name in sorted(_MUTATIONS) if name != mutated]
    before = {name: _digest(**_MUTATIONS[name]) for name in others}

    changed = _digest(**_MUTATIONS[mutated])
    assert changed != baseline, f"{mutated} must change the digest"
    assert changed not in before.values(), f"{mutated} must be a genuinely different input"

    after = {name: _digest(**_MUTATIONS[name]) for name in others}
    assert after == before, "freezing a new input must not disturb another snapshot"


# --------------------------------------------------------------------------- #
# U03
# --------------------------------------------------------------------------- #

def test_u03_mutating_the_original_request_cannot_reach_the_snapshot() -> None:
    messages = _messages()
    tools = _tools()
    request = _build(messages=messages, tools=tools, temperature=0.2, max_tokens=64)
    snapshot = freeze_model_input(request, _context())
    before = snapshot.content_digest

    messages.append({"role": "assistant", "content": "被追加"})
    messages[0]["content"] = "被改写"
    tools[0]["function"]["parameters"]["properties"]["date"]["type"] = "integer"
    tools[0]["function"]["name"] = "renamed"
    # ModelRequest is a frozen dataclass, so this is the worst case: even a forced
    # top-level write on the original request must not reach the snapshot.
    object.__setattr__(request, "response_format", {"type": "json_object"})

    assert snapshot.content_digest == before
    restored = snapshot.to_request()
    assert restored.messages == [
        {"role": "system", "content": "你是助手。\n只回答中文。"},
        {"role": "user", "content": "第一问"},
    ]
    assert restored.tools[0]["function"]["name"] == "get_today_tasks"
    assert restored.tools[0]["function"]["parameters"]["properties"]["date"]["type"] == "string"
    assert restored.response_format is None


# --------------------------------------------------------------------------- #
# U04
# --------------------------------------------------------------------------- #

def test_u04_each_to_request_copy_is_independent() -> None:
    snapshot = _freeze()
    digest = snapshot.content_digest

    first = snapshot.to_request()
    first.messages[0]["content"] = "改掉了"
    first.messages.append({"role": "assistant", "content": "新增"})
    first.tools[0]["function"]["parameters"]["properties"]["date"]["type"] = "integer"
    first.tools[0]["function"]["parameters"]["properties"]["extra"] = {"type": "string"}

    second = snapshot.to_request()
    assert second.messages == _messages()
    assert second.tools == _tools()
    assert snapshot.content_digest == digest

    # No shared mutable sub-object between the two copies.
    assert first.messages is not second.messages
    assert first.messages[0] is not second.messages[0]
    assert first.tools[0] is not second.tools[0]
    assert (
        first.tools[0]["function"]["parameters"]
        is not second.tools[0]["function"]["parameters"]
    )


# --------------------------------------------------------------------------- #
# U05
# --------------------------------------------------------------------------- #

def _envelope(**overrides) -> dict:
    base = json.loads(_freeze().content_json)
    base.update(overrides)
    return base


def test_u05_unknown_schema_version_is_refused() -> None:
    with pytest.raises(UnknownSnapshotVersion):
        deserialize_envelope(_envelope(schema_version="model-input-snapshot-v99"))
    with pytest.raises(UnknownSnapshotVersion):
        deserialize_envelope({"schema_version": "model-input-snapshot-v99"})


def test_u05_missing_and_unknown_fields_are_refused() -> None:
    without_provenance = _envelope()
    without_provenance.pop("provenance")
    with pytest.raises(SnapshotError):
        deserialize_envelope(without_provenance)

    with_extra = _envelope(extra="nope")
    with pytest.raises(SnapshotError):
        deserialize_envelope(with_extra)

    missing_request_field = _envelope()
    missing_request_field["request"].pop("single_attempt")
    with pytest.raises(SnapshotError):
        deserialize_envelope(missing_request_field)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
)
def test_u05_non_finite_numbers_are_refused(value: float) -> None:
    with pytest.raises(SnapshotError):
        freeze_model_input(_build(temperature=value), _context())


@pytest.mark.parametrize("value", [object(), {1, 2}, b"bytes", complex(1, 2), lambda: None])
def test_u05_unsupported_types_are_refused_instead_of_stringified(value: object) -> None:
    request = _build(messages=[{"role": "user", "content": value}])
    with pytest.raises(SnapshotError):
        freeze_model_input(request, _context())

    # A nested position is refused the same way - no silent `str()`.
    nested = _build(tools=[{"type": "function", "function": {"name": "x", "parameters": value}}])
    with pytest.raises(SnapshotError):
        freeze_model_input(nested, _context())


def test_u05_non_string_keys_are_refused() -> None:
    with pytest.raises(SnapshotError):
        freeze_model_input(_build(messages=[{"role": "user", "content": "x", 1: "y"}]), _context())


def test_u05_a_snapshot_cannot_contradict_its_own_binding() -> None:
    snapshot = _freeze()
    with pytest.raises(SnapshotBindingConflict):
        ModelInputSnapshot(
            owner_id="owner-b",  # the envelope says owner-a
            role=snapshot.role,
            purpose=snapshot.purpose,
            runtime_bundle_id=snapshot.runtime_bundle_id,
            content_json=snapshot.content_json,
            content_digest=snapshot.content_digest,
        )
    with pytest.raises(SnapshotBindingConflict):
        ModelInputSnapshot(
            owner_id=snapshot.owner_id,
            role="planner",  # the envelope says conversation
            purpose=snapshot.purpose,
            runtime_bundle_id=snapshot.runtime_bundle_id,
            content_json=snapshot.content_json,
            content_digest=snapshot.content_digest,
        )
    with pytest.raises(SnapshotIntegrityError):
        ModelInputSnapshot(
            owner_id=snapshot.owner_id,
            role=snapshot.role,
            purpose=snapshot.purpose,
            runtime_bundle_id=snapshot.runtime_bundle_id,
            content_json=snapshot.content_json,
            content_digest="0" * 64,
        )


# --------------------------------------------------------------------------- #
# U06
# --------------------------------------------------------------------------- #

def test_u06_round_trip_covers_every_request_field_without_loss() -> None:
    messages = [
        {"role": "system", "content": "中文系统提示\n第二行"},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": "  前后有空格  "},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_abc123", "type": "function", "function": {"name": "get_today_tasks", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_abc123", "content": "[]"},
    ]
    tools = _tools()
    response_format = {"type": "json_schema", "json_schema": {"name": "r", "schema": {"type": "object"}}}
    request = ModelRequest(
        messages=messages,
        tools=tools,
        temperature=0.0,
        max_tokens=4096,
        role="conversation",
        purpose="route_and_respond",
        thinking=False,
        response_format=response_format,
        single_attempt=True,
    )
    snapshot = freeze_model_input(request, _context())
    restored = snapshot.to_request()

    assert restored.messages == messages
    assert [message["role"] for message in restored.messages] == [m["role"] for m in messages]
    assert restored.tools == tools
    assert restored.temperature == 0.0
    assert restored.max_tokens == 4096
    assert restored.role == "conversation"
    assert restored.purpose == "route_and_respond"
    assert restored.thinking is False
    assert restored.response_format == response_format
    assert restored.single_attempt is True

    # `null` and `[]` keep their original difference; neither is rewritten.
    assert freeze_model_input(_build(tools=None), _context()).to_request().tools is None
    assert freeze_model_input(_build(tools=[]), _context()).to_request().tools == []

    # The digest is stable across a re-parse and changes with any field.
    assert snapshot_digest(snapshot.content_json) == snapshot.content_digest
    assert snapshot_digest(snapshot.envelope()) == snapshot.content_digest
    assert canonical_json(snapshot.envelope()) == snapshot.content_json

    # No database identifiers leak into the envelope.
    assert "id" not in snapshot.envelope()
    assert "created_at" not in snapshot.content_json
    assert snapshot.schema_version == SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# U08
# --------------------------------------------------------------------------- #

def test_u08_only_the_memory_that_was_actually_sent_counts_as_included() -> None:
    """Two candidate memories are assembled, one of them is dropped.

    The dropped candidate must be *recorded* with a reason rather than omitted:
    a snapshot that only lists what it sent would look better sourced than it is.
    """
    messages = [
        {"role": "system", "content": "记忆A：用户喜欢晨跑。"},
        {"role": "user", "content": "今天要做什么？"},
    ]
    provenance = build_provenance([
        {
            "kind": "memory", "id": "memory-A", "version": "rev-3",
            "content_digest": "digest-A", "included": True,
            "location": {"role": "system", "index": 0},
        },
        {
            "kind": "memory", "id": "memory-B", "version": "rev-9",
            "content_digest": "digest-B", "included": False,
            "dropped_reason": "budget",
        },
    ])
    snapshot = _freeze(messages=messages, provenance=provenance)

    stored = snapshot.provenance()
    assert stored.status == "partial", "phase 2A never claims complete"
    by_id = {source.id: source for source in stored.sources}
    assert by_id["memory-A"].included is True
    assert by_id["memory-A"].location == {"role": "system", "index": 0}
    assert by_id["memory-A"].fully_tracked is True
    assert by_id["memory-B"].included is False
    assert by_id["memory-B"].dropped_reason == "budget"
    assert by_id["memory-B"].fully_tracked is True

    # The record matches the bytes: A's content is in the frozen input, B's is not.
    rendered = json.dumps(snapshot.request_payload(), ensure_ascii=False)
    assert "晨跑" in rendered
    assert "减脂" not in rendered

    # A source this phase cannot name is kept as `other` instead of being dropped,
    # and an unrecognised source can never be promoted to a `complete` claim.
    unknown = build_provenance([{"kind": "quantum-memory", "id": "memory-X"}])
    assert unknown.status == "partial"
    assert unknown.sources[0].kind == "other"
    assert unknown.sources[0].fully_tracked is False
    with pytest.raises(SnapshotError):
        build_provenance([{"kind": "quantum-memory", "id": "memory-X"}], status="complete")


# --------------------------------------------------------------------------- #
# U09
# --------------------------------------------------------------------------- #

def test_u09_a_complete_input_with_partial_provenance_still_freezes_and_sends() -> None:
    """The final input is whole; only the per-fragment provenance is not.

    Phase 2A records the references the existing chain can name but not the
    position of every history/summary/attachment fragment, so the status stays
    ``partial`` while freezing, persisting and sending all work normally.
    """
    messages = [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "今天要做什么？"},
    ]
    provenance = build_provenance([
        {
            "kind": "skill", "id": "skill_plan", "version": "v7",
            "content_digest": "d-skill", "included": True,
            "location": {"role": "system", "index": 0},
        },
        # Present in the final input, but this phase does not record where each
        # fragment landed, nor a complete dropped list.
        {"kind": "history", "id": "turn_12", "included": True},
        {"kind": "summary", "id": "episode_4", "included": True},
        {"kind": "attachment", "id": "artifact_9", "included": True},
    ])
    snapshot = _freeze(messages=messages, provenance=provenance)

    stored = snapshot.provenance()
    assert stored.status == "partial"
    # The logical input itself is complete and real: what would be sent is exactly
    # the frozen input, not a re-assembled approximation of it.
    assert snapshot.to_request().messages == messages

    # The key reference this phase *can* name survives with its version and digest.
    skill = next(source for source in stored.sources if source.id == "skill_plan")
    assert (skill.version, skill.content_digest) == ("v7", "d-skill")
    assert skill.fully_tracked is True
    # ... and the untracked fragments are exactly what keeps the status honest.
    assert {s.id for s in stored.sources if not s.fully_tracked} == {
        "turn_12", "episode_4", "artifact_9",
    }

    # Persisting and re-parsing keeps both the digest and the status.
    envelope = snapshot.envelope()
    assert canonical_json(envelope) == snapshot.content_json
    assert snapshot_digest(canonical_json(envelope)) == snapshot.content_digest
    assert deserialize_envelope(snapshot.content_json)["provenance"]["status"] == "partial"

    # A stored row cannot be upgraded to `complete` without evidence: the claim is
    # validated on the way in rather than trusted, and a tampered digest is refused.
    tampered = json.loads(snapshot.content_json)
    tampered["provenance"]["status"] = "complete"
    with pytest.raises(SnapshotError):
        deserialize_envelope(tampered)
    with pytest.raises(SnapshotError):
        snapshot_digest(tampered)

    # Freezing the same logical input twice stays byte-identical: `partial` does
    # not make the frozen content non-deterministic.
    assert _freeze(messages=messages, provenance=provenance).content_digest == snapshot.content_digest
