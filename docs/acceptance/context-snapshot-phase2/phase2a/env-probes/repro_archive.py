"""Repro for test_worker_archives_old_history_before_model_and_replaces_raw_source_with_episode."""

import asyncio
import pathlib
import sys
import tempfile
import traceback

BACKEND = r"D:\RAG\better\backend"
sys.path.insert(0, BACKEND)
sys.path.insert(0, BACKEND + r"\tests")

from test_runtime import make_runtime  # noqa: E402
from test_conversation_worker import HistoryRecordingConversationModel  # noqa: E402


async def summarize(payload):
    source_id = payload["turns"][0]["events"][0]["message_id"]
    item = {"text": "用户早先讨论了旧主题", "source_message_ids": [source_id]}
    return {
        "synopsis": [item], "topics": [], "decisions": [], "outcomes": [],
        "open_loops": [], "sensitivity": "normal",
    }


async def main() -> None:
    from app.memory_archive import ConversationArchiver
    from app.memory_v2 import MemoryContextProvider, MemoryStore

    tmp_path = pathlib.Path(tempfile.mkdtemp(dir="C:/tmp", prefix="repro-arch-"))
    model = HistoryRecordingConversationModel(
        '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\nanswer'
    )
    runtime = make_runtime(tmp_path, model)
    store = MemoryStore(runtime.db, tmp_path / "memory-v2")
    runtime.memory_context = MemoryContextProvider(runtime.db)
    runtime.archiver = ConversationArchiver(
        runtime.db, store, summarize, keep_tokens=1_000, max_summary_tokens=12_000,
    )
    thread = runtime.conversation.create_thread("Archive before generation")
    now = "2026-01-01T00:00:00+00:00"
    old_marker = "OLD_RAW_SOURCE_" + ("x" * 2_000)
    with runtime.db.transaction() as connection:
        seeded = [("old-turn", old_marker, "old answer")]
        seeded.extend(
            (f"filler-{index}", f"filler user {index}", f"filler answer {index}")
            for index in range(4)
        )
        seeded.append(("recent-turn", "RECENT_RAW_SOURCE", "recent answer"))
        for index, (turn_id, user_text, assistant_text) in enumerate(seeded):
            connection.execute(
                "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) "
                "VALUES (?,?,?,'COMPLETED',?,?)",
                (turn_id, thread.id, turn_id, now, now),
            )
            for offset, (role, content) in enumerate((("user", user_text), ("assistant", assistant_text))):
                sequence = index * 2 + 1 + offset
                connection.execute(
                    "INSERT INTO thread_messages(id,thread_id,turn_id,role,content,status,generation,"
                    "content_length,message_seq,created_at,completed_at) "
                    "VALUES (?,?,?,?,?,'ready',1,?,?,?,?)",
                    (
                        f"message-{sequence}", thread.id, turn_id, role, content,
                        len(content), sequence, now, now,
                    ),
                )
    accepted = runtime.conversation.accept_turn(thread.id, "current-turn", "CURRENT_REQUEST", [])
    print(f"accepted turn={accepted.turn_id}", flush=True)

    ok = await runtime.turn_worker.run_once()
    print(f"run_once -> {ok!r} model.calls={model.calls}", flush=True)

    with runtime.db.connection() as connection:
        turn = connection.execute(
            "SELECT status,policy,reason_code,content_shape FROM turns WHERE id=?",
            (accepted.turn_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT status,attempts,lease_owner,lease_until FROM turn_jobs WHERE turn_id=?",
            (accepted.turn_id,),
        ).fetchone()
    print("turn:", dict(turn) if turn else None, flush=True)
    print("job :", dict(job) if job else None, flush=True)
    print("-- events --", flush=True)
    for event in runtime.conversation.events.list(thread.id):
        detail = getattr(event, "detail", None) or getattr(event, "payload", None)
        print(f"    {event.type}  {detail}", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException:
        traceback.print_exc()
