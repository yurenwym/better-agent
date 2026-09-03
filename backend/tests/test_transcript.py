from __future__ import annotations

import json

import pytest

from app.db import Database
from app.transcript import CanonicalTurnTranscriptBuilder, ScopeMismatch


def _thread(connection, thread_id: str, owner: str = "alice") -> None:
    connection.execute(
        "INSERT INTO threads(id,title,owner_id,created_at,updated_at) VALUES (?,?,?,?,?)",
        (thread_id, thread_id, owner, "now", "now"),
    )


def _turn(connection, thread_id: str, index: int, *, status: str = "COMPLETED") -> str:
    turn_id = f"{thread_id}-turn-{index}"
    connection.execute(
        "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (turn_id, thread_id, turn_id, status, "now", "now"),
    )
    return turn_id


def _message(connection, thread_id: str, turn_id: str, seq: int, role: str, content: str, *, status: str = "ready", generation: int = 1) -> str:
    message_id = f"{thread_id}-message-{seq}-{generation}"
    connection.execute(
        "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,content_length,message_seq,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (message_id, thread_id, turn_id, role, content, status, generation, len(content), seq, "now"),
    )
    return message_id


def test_transcript_selects_newest_complete_turns_without_message_limit(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        _thread(connection, "thread")
        for index in range(30):
            turn_id = _turn(connection, "thread", index)
            _message(connection, "thread", turn_id, index * 2 + 1, "user", f"user-{index}")
            _message(connection, "thread", turn_id, index * 2 + 2, "assistant", f"assistant-{index}")

    builder = CanonicalTurnTranscriptBuilder(db)
    transcript = builder.pack_recent(builder.build("thread"), token_budget=900)
    history = builder.render_history(transcript)

    assert history[-2:] == [
        {"role": "user", "content": "user-29"},
        {"role": "assistant", "content": "assistant-29"},
    ]
    assert len(history) % 2 == 0
    assert history[0]["role"] == "user"


def test_transcript_excludes_interrupted_and_failed_assistant_generations(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        _thread(connection, "thread")
        completed = _turn(connection, "thread", 1)
        _message(connection, "thread", completed, 1, "user", "question")
        _message(connection, "thread", completed, 2, "assistant", "partial", status="interrupted", generation=1)
        _message(connection, "thread", completed, 3, "assistant", "final", generation=2)
        failed = _turn(connection, "thread", 2, status="FAILED")
        _message(connection, "thread", failed, 4, "user", "failed question")
        _message(connection, "thread", failed, 5, "assistant", "failed partial", status="ready")

    history = CanonicalTurnTranscriptBuilder.render_history(CanonicalTurnTranscriptBuilder(db).build("thread"))

    assert [item["content"] for item in history] == ["question", "final", "failed question"]


def test_transcript_scopes_asks_to_thread_and_renders_each_once(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        for thread_id in ("a", "b"):
            _thread(connection, thread_id)
            turn_id = _turn(connection, thread_id, 1)
            _message(connection, thread_id, turn_id, 1, "user", f"question-{thread_id}")
            assistant_id = _message(connection, thread_id, turn_id, 2, "assistant", f"answer-{thread_id}")
            for index in range(2):
                ask_id = f"ask-{thread_id}-{index}"
                connection.execute(
                    "INSERT INTO turn_asks(id,turn_id,call_id,questions_json,status,answer_json,created_at,answered_at) "
                    "VALUES (?,?,?,?, 'ANSWERED',?,?,?)",
                    (
                        ask_id, turn_id, f"call-{thread_id}-{index}",
                        json.dumps([{
                            "id": f"q-{index}", "header": f"header-{index}",
                            "question": f"prompt-{index}", "options": [],
                            "multi_select": False, "allow_free_text": True,
                        }]),
                        json.dumps([{
                            "question_id": f"q-{index}", "selected_options": [],
                            "free_text": f"value-{index}",
                        }]), "now", "now",
                    ),
                )

    history = CanonicalTurnTranscriptBuilder.render_history(CanonicalTurnTranscriptBuilder(db).build("a"))
    calls = [call for item in history for call in item.get("tool_calls", [])]
    results = [item for item in history if item["role"] == "tool"]

    assert {call["id"] for call in calls} == {"call-a-0", "call-a-1"}
    assert {item["tool_call_id"] for item in results} == {"call-a-0", "call-a-1"}
    assert all("call-b" not in json.dumps(item) for item in history)


def test_answered_ask_content_is_part_of_transcript_hash(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        _thread(connection, "thread")
        turn_id = _turn(connection, "thread", 1)
        _message(connection, "thread", turn_id, 1, "user", "question")
        _message(connection, "thread", turn_id, 2, "assistant", "answer")
        connection.execute(
            "INSERT INTO turn_asks(id,turn_id,call_id,questions_json,status,answer_json,created_at,answered_at) "
            "VALUES (?,?,?,?, 'ANSWERED',?,?,?)",
            (
                "ask", turn_id, "call", json.dumps([{
                    "id": "q", "header": "h", "question": "prompt", "options": [],
                    "multi_select": False, "allow_free_text": True,
                }]), json.dumps([{
                    "question_id": "q", "selected_options": [], "free_text": "first",
                }]), "now", "now",
            ),
        )
    builder = CanonicalTurnTranscriptBuilder(db)
    first = builder.build("thread")
    with db.transaction() as connection:
        connection.execute(
            "UPDATE turn_asks SET answer_json=? WHERE id=?",
            (json.dumps([{"question_id": "q", "selected_options": [], "free_text": "second"}]), "ask"),
        )
    second = builder.build("thread")
    assert first.source_hash != second.source_hash


def test_message_ids_are_unique_when_assistant_message_has_ask_events(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        _thread(connection, "thread")
        turn_id = _turn(connection, "thread", 1)
        _message(connection, "thread", turn_id, 1, "user", "question")
        _message(connection, "thread", turn_id, 2, "assistant", "answer")
        connection.execute(
            "INSERT INTO turn_asks(id,turn_id,call_id,questions_json,status,answer_json,created_at,answered_at) VALUES (?,?,?,?, 'ANSWERED',?,?,?)",
            (
                "ask", turn_id, "call", json.dumps([{"id":"q","header":"h","question":"p","options":[],"multi_select":False,"allow_free_text":True}]),
                json.dumps([{"question_id":"q","selected_options":[],"free_text":"a"}]), "now", "now",
            ),
        )
    transcript = CanonicalTurnTranscriptBuilder(db).build("thread")
    assert transcript.message_ids == ("thread-message-1-1", "thread-message-2-1")


def test_scope_is_resolved_from_database(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        _thread(connection, "thread", owner="alice")
    builder = CanonicalTurnTranscriptBuilder(db)
    assert builder.resolve_scope("thread", "alice").owner_id == "alice"
    with pytest.raises(ScopeMismatch):
        builder.resolve_scope("thread", "bob")


def test_transcript_rejects_message_turn_join_from_another_thread(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        _thread(connection, "a")
        _thread(connection, "b")
        turn_a = _turn(connection, "a", 1)
        turn_b = _turn(connection, "b", 1)
        # Deliberately create an inconsistent row.  The canonical transcript
        # must not make it visible in either thread.
        _message(connection, "a", turn_b, 1, "user", "cross-thread")
        _message(connection, "a", turn_a, 2, "user", "valid")

    transcript = CanonicalTurnTranscriptBuilder(db).build("a")
    history = CanonicalTurnTranscriptBuilder.render_history(transcript)
    assert history == [{"role": "user", "content": "valid"}]
