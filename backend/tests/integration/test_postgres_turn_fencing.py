from __future__ import annotations

import asyncio

import pytest


class ScriptedConversationModel:
    def __init__(self, response: str) -> None:
        self.response = response

    async def route_and_respond(self, *, on_text_delta, **kwargs) -> None:
        on_text_delta(self.response)


def _runtime(tmp_path, database_url, response: str):
    from app.startup import build_runtime

    return build_runtime(
        tmp_path,
        database_url=database_url,
        conversation_model=ScriptedConversationModel(response),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "side_effect_table"),
    [
        (
            '{"v":3,"policy":"start_research","content_shape":"research",'
            '"reason_code":"explicit_deep_research","research":{"topic":"topic","scope":"web"}}\n',
            "research_jobs",
        ),
        (
            '{"v":4,"policy":"start_expert","content_shape":"expert",'
            '"reason_code":"complex_compare","expert":{"objective":"compare","roles":["critic"]}}\n',
            "agent_runs",
        ),
    ],
)
async def test_postgres_cancel_wins_before_durable_side_effect_commit(
    migrated_postgres_url, tmp_path, monkeypatch, response, side_effect_table,
) -> None:
    from app.conversation import ControlHeadDecoder

    runtime = _runtime(tmp_path, migrated_postgres_url, response)
    try:
        thread = runtime.conversation.create_thread("cancel wins")
        accepted = runtime.conversation.accept_turn(thread.id, "cancel-wins", "work", [])
        original_finish = ControlHeadDecoder.finish

        def cancel_after_decode(decoder):
            original_finish(decoder)
            runtime.conversation.cancel_turn(accepted.turn_id)

        monkeypatch.setattr(ControlHeadDecoder, "finish", cancel_after_decode)

        await runtime.turn_worker.run_once()

        assert runtime.conversation.turn(accepted.turn_id).status == "CANCELLED"
        with runtime.db.connection() as connection:
            assert connection.execute(
                f"SELECT COUNT(*) FROM {side_effect_table}"
            ).fetchone()[0] == 0
    finally:
        runtime.db.close()


@pytest.mark.asyncio
async def test_postgres_completed_commit_cannot_report_late_cancel_as_accepted(
    migrated_postgres_url, tmp_path,
) -> None:
    response = (
        '{"v":3,"policy":"start_research","content_shape":"research",'
        '"reason_code":"explicit_deep_research","research":{"topic":"topic","scope":"web"}}\n'
    )
    runtime = _runtime(tmp_path, migrated_postgres_url, response)
    original_create = runtime.research.create_from_turn
    cancel_task = None
    errors = []
    try:
        thread = runtime.conversation.create_thread("commit wins")
        accepted = runtime.conversation.accept_turn(thread.id, "commit-wins", "work", [])

        def start_late_cancel(*args, **kwargs):
            nonlocal cancel_task
            try:
                cancel_task = asyncio.get_running_loop().run_in_executor(
                    None, runtime.conversation.cancel_turn, accepted.turn_id,
                )
                return original_create(*args, **kwargs)
            except Exception as exc:
                errors.append(repr(exc))
                raise RuntimeError(f"research create failed: {exc!r}") from exc

        runtime.research.create_from_turn = start_late_cancel
        await runtime.turn_worker.run_once()
        cancelled = await asyncio.wait_for(cancel_task, timeout=2)

        current = runtime.conversation.turn(accepted.turn_id)
        assert (cancelled.status, current.status) == ("COMPLETED", "COMPLETED"), errors or [
            (event.type, event.data) for event in runtime.conversation.events.list(thread.id)
            if event.type in {"turn.failed", "turn.cancelled"}
        ]
        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM research_jobs").fetchone()[0] == 1
    finally:
        runtime.db.close()


def test_postgres_stale_epoch_cannot_persist_terminal_metrics(
    migrated_postgres_url, tmp_path,
) -> None:
    from app.conversation import ManagedTurnWorker

    runtime = _runtime(
        tmp_path,
        migrated_postgres_url,
        '{"v":1,"policy":"answer","content_shape":"general","reason_code":"content_only"}\nanswer',
    )
    try:
        thread = runtime.conversation.create_thread("stale metrics")
        accepted = runtime.conversation.accept_turn(thread.id, "stale-metrics", "answer", [])
        worker = ManagedTurnWorker(runtime.conversation, owner="old-worker")
        assert worker.claim_next() == accepted.turn_id
        old_epoch = worker._lease_token.epoch
        with runtime.db.transaction() as connection:
            connection.execute(
                "UPDATE turns SET status='COMPLETED',updated_at=clock_timestamp() WHERE id=%s",
                (accepted.turn_id,),
            )
            connection.execute(
                "UPDATE turn_jobs SET status='COMPLETED',lease_epoch=lease_epoch+1,lease_owner=NULL,"
                "lease_until=NULL,finished_at=clock_timestamp() WHERE turn_id=%s",
                (accepted.turn_id,),
            )

        worker._persist_terminal_metrics(
            accepted.turn_id, lease_epoch=old_epoch, context_ms=1, model_ttft_ms=1,
        )

        with runtime.db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM turn_metrics").fetchone()[0] == 0
    finally:
        runtime.db.close()
