from app.db import Database
import pytest


def test_conversation_schema_is_durable_and_migrates_source_turn_id(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")

    with db.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"threads", "turns", "turn_jobs", "thread_messages", "thread_events"} <= tables
        run_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        assert "source_turn_id" in run_columns
        turn_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(turns)").fetchall()
        }
        assert "skill_names_json" in turn_columns


def test_conversation_transaction_rolls_back_all_rows(tmp_path) -> None:
    db = Database(tmp_path / "agent.db")

    try:
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO threads(id, title, version, active_turn_id, next_event_seq, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("thread-1", "Test", 0, None, 1, "now", "now"),
            )
            connection.execute(
                "INSERT INTO turns(id, thread_id, client_turn_id, status, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("turn-1", "thread-1", "client-1", "ACCEPTED", 0, "now", "now"),
            )
            raise RuntimeError("rollback")
    except RuntimeError:
        pass

    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0


def test_thread_event_store_uses_persisted_thread_cursor(tmp_path) -> None:
    from app.events import ThreadEventStore

    db = Database(tmp_path / "agent.db")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id, title, version, next_event_seq, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("thread-1", "Test", 0, 1, "now", "now"),
        )

    events = ThreadEventStore(db)
    first = events.append("thread-1", "turn-1", "turn.accepted", "user", {})
    second = events.append("thread-1", "turn-1", "turn.started", "worker", {})

    assert [event.seq for event in events.list("thread-1")] == [1, 2]
    assert first.event_id != second.event_id


def test_control_head_releases_only_markdown_after_valid_json_line() -> None:
    from app.conversation import ControlHeadDecoder, RouteDecision

    decoder = ControlHeadDecoder(max_header_bytes=1024)
    assert decoder.feed('{"v":1,"policy":"answer","content_shape":"guide",') == ""
    assert decoder.feed('"reason_code":"content_only"}\n## 桂林') == "## 桂林"
    assert decoder.header == RouteDecision("answer", "guide", "content_only")
    assert decoder.feed(" 7 天攻略") == " 7 天攻略"


def test_control_head_rejects_unknown_policy_without_repair() -> None:
    from app.conversation import ControlHeadDecoder, RouteProtocolError

    decoder = ControlHeadDecoder()
    with pytest.raises(RouteProtocolError):
        decoder.feed('{"v":1,"policy":"goal"}\n')


def test_control_head_rejects_unknown_fields_and_missing_header() -> None:
    from app.conversation import ControlHeadDecoder, RouteProtocolError

    with pytest.raises(RouteProtocolError):
        ControlHeadDecoder().feed(
            '{"v":1,"policy":"answer","content_shape":"guide",'
            '"reason_code":"content_only","needs_user_choice":true}\n'
        )
    with pytest.raises(RouteProtocolError):
        ControlHeadDecoder().finish()


@pytest.mark.asyncio
async def test_live_conversation_model_uses_one_tool_free_request() -> None:
    from app.live_model import LiveConversationModel

    class Gateway:
        def __init__(self) -> None:
            self.calls = 0
            self.request = None

        async def complete(self, request, **kwargs):
            self.calls += 1
            self.request = request
            return "response"

    gateway = Gateway()
    model = LiveConversationModel(gateway)
    result = await model.route_and_respond(
        content="给我一份攻略",
        history=[],
        skill_names=[],
        on_text_delta=None,
        on_text_reset=None,
        cancel_event=None,
    )

    assert result == "response"
    assert gateway.calls == 1
    assert gateway.request.tools == []
