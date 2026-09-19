import sqlite3
import time
from dataclasses import dataclass

from fastapi.testclient import TestClient


ANSWER = "PostgreSQL 中持久化的完整回答。"


@dataclass
class FixedEmbeddingProvider:
    dimensions: int = 1024

    def embed(self, texts):
        from app.embedding import EmbeddingBatch

        vectors = tuple((1.0, *([0.0] * (self.dimensions - 1))) for _ in texts)
        return EmbeddingBatch(vectors, "Qwen/Qwen3-Embedding-4B", self.dimensions)


class DeterministicStreamingConversationModel:
    async def route_and_respond(self, *, on_text_delta, **kwargs) -> None:
        response = (
            '{"v":1,"policy":"answer","content_shape":"general",'
            '"reason_code":"content_only"}\n'
            + ANSWER
        )
        midpoint = len(response) // 2
        on_text_delta(response[:midpoint])
        on_text_delta(response[midpoint:])


def _headers(app, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
    }
    if idempotency_key is not None:
        headers["idempotency-key"] = idempotency_key
    return headers


def _wait_for_terminal(client: TestClient, thread_id: str, timeout_s: float = 10) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/threads/{thread_id}", headers={"host": "127.0.0.1:8000"}
        )
        response.raise_for_status()
        turn = response.json()["turns"][-1]
        metrics = turn.get("metrics") or {}
        if turn["status"] in {"COMPLETED", "FAILED", "CANCELLED"} and all(
            metrics.get(name) is not None
            for name in ("queue_wait_ms", "context_ms", "model_ttft_ms", "stream_ms")
        ):
            return turn
        time.sleep(0.02)
    raise AssertionError("turn did not reach a terminal state")


def _build_runtime(data_root, database_url):
    from app.startup import build_runtime

    return build_runtime(
        data_root,
        database_url=database_url,
        conversation_model=DeterministicStreamingConversationModel(),
    )


def test_a17_single_conversation_survives_postgres_backed_restart(
    migrated_postgres_url, tmp_path, monkeypatch, request
) -> None:
    from app.main import create_app

    def reject_sqlite(*args, **kwargs):
        raise AssertionError("A17 PostgreSQL flow attempted to access SQLite")

    monkeypatch.setattr(sqlite3, "connect", reject_sqlite)
    data_root = tmp_path / "data"
    runtime = _build_runtime(data_root, migrated_postgres_url)
    request.addfinalizer(runtime.db.close)
    assert runtime.db.backend == "postgresql"
    embedding_provider = FixedEmbeddingProvider()
    runtime.memory_store.remember(
        "local-user",
        "fact",
        "user",
        "",
        "请给出一个简短回答",
        "a17-memory",
    )
    from app.embedding_worker import EmbeddingWorker
    from app.memory_v2 import MemoryContextProvider

    assert EmbeddingWorker(
        runtime.db, embedding_provider, owner="a17-embedding"
    ).run_once()
    runtime.memory_context = MemoryContextProvider(
        runtime.db, embedding_provider=embedding_provider
    )

    app = create_app(runtime=runtime)
    with TestClient(app) as client:
        created = client.post(
            "/api/threads",
            json={"title": "A17 PostgreSQL conversation"},
            headers=_headers(app, idempotency_key="a17-thread"),
        )
        assert created.status_code == 201, created.text
        thread_id = created.json()["id"]

        accepted = client.post(
            f"/api/threads/{thread_id}/turns",
            json={
                "client_turn_id": "a17-turn-1",
                "content": "请给出一个简短回答",
                "skill_names": [],
            },
            headers=_headers(app, idempotency_key="a17-turn"),
        )
        assert accepted.status_code == 202, accepted.text

        turn = _wait_for_terminal(client, thread_id)
        diagnostic_events = client.get(
            f"/api/threads/{thread_id}/events",
            headers={"host": "127.0.0.1:8000"},
        ).json()["events"]
        assert turn["status"] == "COMPLETED", diagnostic_events
        assert all(
            isinstance(turn["metrics"][name], int) and turn["metrics"][name] >= 0
            for name in ("queue_wait_ms", "context_ms", "model_ttft_ms", "stream_ms")
        )

        messages_response = client.get(
            f"/api/threads/{thread_id}/messages",
            headers={"host": "127.0.0.1:8000"},
        )
        messages_response.raise_for_status()
        messages = messages_response.json()["messages"]
        assert [(message["role"], message["status"]) for message in messages] == [
            ("user", "ready"),
            ("assistant", "ready"),
        ]
        assert messages[-1]["content"] == ANSWER

        events_response = client.get(
            f"/api/threads/{thread_id}/events",
            headers={"host": "127.0.0.1:8000"},
        )
        events_response.raise_for_status()
        events = events_response.json()["events"]
        event_types = [event["type"] for event in events]
        assert "turn.started" in event_types
        assert "message.delta" in event_types
        assert "message.completed" in event_types
        assert "turn.completed" in event_types
        assert "turn.metrics.updated" in event_types
        retrieval = next(event for event in events if event["type"] == "memory.retrieval.completed")
        assert retrieval["data"]["retrieval_mode"] == "hybrid"
        assert retrieval["data"]["semantic_candidate_count"] >= 1
        assert not any(
            marker in event_type
            for event_type in event_types
            for marker in ("failed", "error", "dead_letter")
        )

    restarted = _build_runtime(data_root, migrated_postgres_url)
    request.addfinalizer(restarted.db.close)
    restarted_app = create_app(runtime=restarted)
    with TestClient(restarted_app) as client:
        thread_response = client.get(
            f"/api/threads/{thread_id}", headers={"host": "127.0.0.1:8000"}
        )
        thread_response.raise_for_status()
        assert thread_response.json()["turns"][-1]["status"] == "COMPLETED"
        messages_response = client.get(
            f"/api/threads/{thread_id}/messages",
            headers={"host": "127.0.0.1:8000"},
        )
        messages_response.raise_for_status()
        assert messages_response.json()["messages"][-1]["content"] == ANSWER

    assert not (data_root / "agent.db").exists()
