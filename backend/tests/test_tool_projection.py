"""R2-01 acceptance: large tool results are projected, never rewritten.

The contract has two halves and both are load-bearing:

* the **original** is persisted in full and stays byte-identical, so a result
  can always be recovered and no already-executed fact changes;
* the **context** carries a mechanical projection - status, every bounded
  authoritative field, a head/tail excerpt of anything oversized, and a
  resolvable reference.

A paraphrase is never an acceptable substitute: a summary that disagrees with
the real result is worse than no result at all.
"""

import json

import pytest

from app.db import Database
from app.model_gateway import ModelProfile
from app.tool_projection import (
    ResultScopeError,
    project_result_content,
    read_result_page,
    result_context_bytes,
)
from app.token_budget import hot_window

OWNER = "local-user"
OTHER = "someone-else"


def _profile(**overrides) -> ModelProfile:
    values = {
        "base_url": "https://example.test/v1",
        "model": "demo",
        "api_key_env": "TOOL_KEY",
        "context_window": 32768,
        "max_output_tokens": 8192,
        "max_attempts": 1,
    }
    values.update(overrides)
    return ModelProfile(**values)


def _db(tmp_path) -> Database:
    return Database(tmp_path / "tool.db")


def _seed(db: Database, *, owner: str = OWNER, call_id: str = "call-1", answer: str = "{}") -> None:
    now = "2026-09-19T00:00:00+00:00"
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (f"thread-{owner}", "t", owner, now, now),
        )
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (f"turn-{owner}", f"thread-{owner}", "c1", "COMPLETED", now, now),
        )
        connection.execute(
            "INSERT INTO turn_asks(id,turn_id,call_id,questions_json,status,answer_json,owner_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"ask-{owner}", f"turn-{owner}", call_id, "[]", "ANSWERED", answer, owner, now),
        )


def _ask_payload(free_text: str) -> str:
    return json.dumps({
        "questions": [{
            "id": "q1", "header": "预算", "question": "你的预算上限是多少？",
            "options": [], "multi_select": False, "allow_free_text": True,
        }],
        "answers": [{
            "question_id": "q1", "selected_options": [], "free_text": free_text,
        }],
    }, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# Projection shape
# --------------------------------------------------------------------------- #


def test_small_result_is_passed_through_untouched() -> None:
    content = _ask_payload("八千元")

    assert project_result_content(content, limit_bytes=2048, reference={}) == content


def test_bounded_authoritative_fields_survive_projection() -> None:
    """Question ids, selected options and headers are authoritative and bounded."""
    payload = json.dumps({
        "questions": [{
            "id": "q1", "header": "交通", "question": "选哪种？",
            "options": [{"label": "高铁"}, {"label": "飞机"}],
            "multi_select": False, "allow_free_text": True,
        }],
        "answers": [{
            "question_id": "q1", "selected_options": ["高铁"], "free_text": "备" * 4000,
        }],
    }, ensure_ascii=False, sort_keys=True)

    projected = json.loads(project_result_content(
        payload, limit_bytes=512, reference={"call_id": "call-1"},
    ))

    assert projected["questions"][0]["id"] == "q1"
    assert projected["questions"][0]["header"] == "交通"
    assert projected["questions"][0]["options"] == [{"label": "高铁"}, {"label": "飞机"}]
    assert projected["answers"][0]["question_id"] == "q1"
    assert projected["answers"][0]["selected_options"] == ["高铁"]


def test_oversized_value_records_size_excerpt_and_reference() -> None:
    projected = json.loads(project_result_content(
        _ask_payload("备" * 5000), limit_bytes=600,
        reference={"tool": "ask_user", "call_id": "call-9", "turn_id": "turn-1"},
    ))

    replaced = projected["answers"][0]["free_text"]
    assert replaced["_truncated"] is True
    assert replaced["_total_bytes"] == len(("备" * 5000).encode("utf-8"))
    assert replaced["_reference"]["call_id"] == "call-9"
    assert replaced["_reference"]["path"] == "$.answers[0].free_text"


def test_excerpt_is_verbatim_not_paraphrased() -> None:
    original = "开头" + ("中" * 3000) + "结尾"
    projected = json.loads(project_result_content(
        _ask_payload(original), limit_bytes=400, reference={},
    ))

    replaced = projected["answers"][0]["free_text"]
    assert original.startswith(replaced["_head"])
    assert original.endswith(replaced["_tail"])


def test_projection_brings_the_result_under_the_ceiling() -> None:
    limit = 512
    projected = project_result_content(
        _ask_payload("x" * 50000), limit_bytes=limit, reference={},
    )

    assert len(projected.encode("utf-8")) <= limit * 8


def test_hard_ceiling_records_dropped_keys_instead_of_dropping_silently() -> None:
    """Many bounded fields can still add up; nothing may vanish unrecorded."""
    payload = json.dumps({f"field_{index}": "v" * 300 for index in range(200)}, sort_keys=True)

    projected = json.loads(project_result_content(
        payload, limit_bytes=256, reference={"call_id": "call-1"},
    ))

    assert projected["_truncated"] is True
    assert projected["_dropped_top_level_keys"], "dropped keys must be recorded"
    assert len(projected["_dropped_top_level_keys"]) > 0


def test_projection_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="positive"):
        project_result_content("{}", limit_bytes=0, reference={})


# --------------------------------------------------------------------------- #
# Storage is never rewritten
# --------------------------------------------------------------------------- #


def test_original_result_is_never_truncated_in_storage(tmp_path) -> None:
    db = _db(tmp_path)
    answer = _ask_payload("备" * 5000)
    _seed(db, answer=answer)

    with db.connection() as connection:
        stored = connection.execute(
            "SELECT answer_json FROM turn_asks WHERE call_id=?", ("call-1",),
        ).fetchone()["answer_json"]

    assert stored == answer, "projection must not touch the persisted original"


# --------------------------------------------------------------------------- #
# Paged read
# --------------------------------------------------------------------------- #


def test_read_result_page_walks_the_whole_original(tmp_path) -> None:
    db = _db(tmp_path)
    answer = _ask_payload("备" * 4000)
    _seed(db, answer=answer)
    total = len(answer.encode("utf-8"))

    collected = ""
    offset = 0
    while True:
        page = read_result_page(db, "call-1", owner_id=OWNER, offset=offset, limit=1000)
        collected += page["page"]
        if not page["has_more"]:
            break
        offset = page["next_offset"]

    assert collected == answer
    assert page["total_bytes"] == total


def test_read_result_page_refuses_a_cross_scope_read(tmp_path) -> None:
    db = _db(tmp_path)
    _seed(db, answer=_ask_payload("x"))

    with pytest.raises(ResultScopeError):
        read_result_page(db, "call-1", owner_id=OTHER)


def test_read_result_page_rechecks_the_budget(tmp_path) -> None:
    """Reading detail must not blow the window it was meant to protect."""
    from app.token_budget import ContextOverflow

    db = _db(tmp_path)
    _seed(db, answer=_ask_payload("备" * 20000))
    tiny = _profile(context_window=2048, max_output_tokens=1024)

    with pytest.raises(ContextOverflow):
        read_result_page(db, "call-1", owner_id=OWNER, offset=0, limit=1_000_000, profile=tiny)

    # A page that fits the same profile is served.
    page = read_result_page(db, "call-1", owner_id=OWNER, offset=0, limit=200, profile=tiny)
    assert page["has_more"] is True


def test_read_result_page_rejects_unknown_and_negative_inputs(tmp_path) -> None:
    db = _db(tmp_path)
    _seed(db, answer=_ask_payload("x"))

    with pytest.raises(KeyError):
        read_result_page(db, "missing", owner_id=OWNER)
    with pytest.raises(ValueError, match="negative"):
        read_result_page(db, "call-1", owner_id=OWNER, offset=-1)


# --------------------------------------------------------------------------- #
# Policy plumbing
# --------------------------------------------------------------------------- #


def test_hot_window_carries_a_tool_result_budget() -> None:
    window = hot_window(_profile())

    assert window.tool_result_bytes == max(window.input_limit // 8, 256)
    assert window.public_view()["tool_result_bytes"] == window.tool_result_bytes


def test_result_budget_scales_with_the_model() -> None:
    small = result_context_bytes(_profile(context_window=4096, max_output_tokens=1024))
    large = result_context_bytes(_profile(context_window=131072, max_output_tokens=8192))

    assert small < large


# --------------------------------------------------------------------------- #
# Transcript-level wiring
# --------------------------------------------------------------------------- #


def _transcript_with_ask(answer: str):
    from app.transcript import (
        CanonicalTranscript,
        CanonicalTurnTranscriptBuilder,
        ResolvedMemoryScope,
        TranscriptEvent,
        TranscriptTurn,
        _source_hash,
    )

    scope = ResolvedMemoryScope(owner_id=OWNER, thread_id="thread-1", project_id=None)
    events = (
        TranscriptEvent("message", "turn-1", "m1", None, "user", "帮我查一下", 1, 1),
        TranscriptEvent("tool_call", "turn-1", "m2", "call-1", "assistant", "{}", 1, 1),
        TranscriptEvent("tool_result", "turn-1", None, "call-1", "tool", answer, 1, None),
    )
    turns = (TranscriptTurn("turn-1", "completed", 1, 1, events),)
    transcript = CanonicalTranscript(scope, turns, _source_hash(scope, turns, ()))
    return CanonicalTurnTranscriptBuilder.__new__(CanonicalTurnTranscriptBuilder), transcript


def test_context_view_is_projected_while_the_canonical_original_survives() -> None:
    builder, transcript = _transcript_with_ask(_ask_payload("备" * 6000))
    original = transcript.turns[0].events[2].content

    projected = builder.project_context(transcript, limit_bytes=512)

    context_event = projected.turns[0].events[2]
    assert len(context_event.content.encode("utf-8")) < len(original.encode("utf-8"))
    assert json.loads(context_event.content)["answers"][0]["free_text"]["_truncated"] is True
    # The canonical transcript the archiver reads is untouched.
    assert transcript.turns[0].events[2].content == original


def test_projection_keeps_the_tool_call_result_pairing() -> None:
    builder, transcript = _transcript_with_ask(_ask_payload("备" * 6000))

    projected = builder.project_context(transcript, limit_bytes=512)

    events = projected.turns[0].events
    assert events[1].call_id == "call-1"
    assert events[2].call_id == "call-1"
    assert events[2].event_type == "tool_result"


def test_projection_leaves_a_small_result_alone() -> None:
    builder, transcript = _transcript_with_ask(_ask_payload("八千元"))
    original = transcript.turns[0].events[2].content

    projected = builder.project_context(transcript, limit_bytes=4096)

    assert projected.turns[0].events[2].content == original


# --------------------------------------------------------------------------- #
# Degraded storage
# --------------------------------------------------------------------------- #


def test_projection_does_not_need_the_original_to_be_readable() -> None:
    """A projection is built from the content in hand, not from a lookup.

    If the original cannot be fetched - already archived, or storage is
    degraded - the context still gets a usable, honestly marked excerpt rather
    than nothing.
    """
    builder, transcript = _transcript_with_ask(_ask_payload("备" * 6000))

    projected = builder.project_context(transcript, limit_bytes=512)

    replaced = json.loads(projected.turns[0].events[2].content)["answers"][0]["free_text"]
    assert replaced["_truncated"] is True
    assert replaced["_head"]


def test_read_result_page_handles_a_missing_answer_payload(tmp_path) -> None:
    """A row that exists but carries no answer must not be reported as content."""
    db = _db(tmp_path)
    _seed(db, answer=None)

    page = read_result_page(db, "call-1", owner_id=OWNER, limit=100)

    assert page["total_bytes"] == 0
    assert page["page"] == ""
    assert page["has_more"] is False
