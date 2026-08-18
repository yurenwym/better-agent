import json


def test_redacted_jsonl_hides_secrets_and_outside_paths(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore, export_jsonl

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.txt"
    secret = "super-secret-value"
    store = EventStore(Database(tmp_path / "agent.db"), workspace=workspace)
    store.append(
        "run-1",
        "goal-1",
        "model.attempt.finished",
        "model",
        {
            "api_key": secret,
            "authorization": f"Bearer {secret}",
            "cookie": secret,
            "password": secret,
            "env_value": secret,
            "path": str(outside),
            "content": "private note body",
            "summary": "safe summary",
        },
    )

    line = export_jsonl(store.list("run-1"), mode="redacted", workspace=workspace)
    decoded = json.loads(line)

    assert secret not in line
    assert decoded["data"]["summary"] == "safe summary"
    assert decoded["data"]["content"] == {"length": len("private note body"), "redacted": True}
    assert decoded["data"]["path"] == "<outside-workspace>"
    assert "api_key" not in decoded["data"]
    assert "authorization" not in decoded["data"]
    assert "cookie" not in decoded["data"]
    assert "password" not in decoded["data"]


def test_full_export_is_explicit_and_keeps_event_shape(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore, export_jsonl

    store = EventStore(Database(tmp_path / "agent.db"))
    store.append("run-1", "goal-1", "note.created", "user", {"content": "hello"})

    line = export_jsonl(store.list("run-1"), mode="full", workspace=tmp_path)

    assert json.loads(line)["data"]["content"] == "hello"


def test_redacted_jsonl_hides_streamed_delta_text(tmp_path) -> None:
    from app.db import Database
    from app.events import EventStore, export_jsonl

    secret_fragment = '{"summary":"private streamed result"}'
    store = EventStore(Database(tmp_path / "agent.db"))
    store.append("run-1", "goal-1", "model.response.delta", "model", {"delta": secret_fragment})

    line = export_jsonl(store.list("run-1"), mode="redacted", workspace=tmp_path)

    assert secret_fragment not in line
    assert json.loads(line)["data"]["delta"] == {"length": len(secret_fragment), "redacted": True}

