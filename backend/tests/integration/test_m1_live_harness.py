from __future__ import annotations

import json
import time

import pytest


def test_cost_settlement_releases_reservation_on_postgres(migrated_postgres_url, tmp_path):
    from app.costs import CostService
    from app.db import Database

    database = Database(migrated_postgres_url, workspace=tmp_path / "cost-workspace")
    try:
        costs = CostService(database)
        costs.set_budget("m1-cost-owner", "DAILY", "2026-09-08", 100)
        costs.reserve("m1-cost-owner", "DAILY", "2026-09-08", "inv", "att", 70, "reserve")
        costs.settle("m1-cost-owner", "DAILY", "2026-09-08", "inv", "att", "price", 30, "ESTIMATED_COMPLETE")
        assert costs.summary("m1-cost-owner", "DAILY", "2026-09-08") == {
            "limit_microusd": 100, "reserved_microusd": 0, "charged_microusd": 30,
        }
    finally:
        database.close()


@pytest.mark.asyncio
async def test_live_harness_exercises_one_complete_round_without_network(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    from app.embedding import EmbeddingBatch
    from app.model_gateway import ModelProfile, ModelResponse, Timing, UsageBuckets
    from scripts import m1_live_acceptance as live

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "offline-test-key")
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    monkeypatch.setenv("EMBEDDING_API_KEY_ENV", "AGENT_MODEL_API_KEY")
    monkeypatch.setenv("EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-4B")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1024")

    class FakeEmbeddingClient:
        def __init__(self, _profile):
            assert _profile.timeout_seconds == live.EMBEDDING_TIMEOUT_SECONDS
            pass

        def embed(self, texts):
            vectors = tuple((1.0, *([0.0] * 1023)) for _ in texts)
            return EmbeddingBatch(vectors, "Qwen/Qwen3-Embedding-4B", 1024, len(texts), len(texts))

        def close(self):
            pass

    class FakeBudget(live.BatchBudget):
        async def model_attempt(self, _profile, request, **kwargs):
            started = time.perf_counter()
            purpose = request.purpose
            if purpose == "route_and_respond":
                has_memory = any("长期默认数据库是PostgreSQL" in item["content"] for item in request.messages)
                body = (
                    "本次使用SQLite；长期默认数据库仍为PostgreSQL；自行车锻炼强度递增上限为10%；训练恢复日是周五。"
                    if has_memory else "本次使用SQLite；其余信息在当前消息中未知。"
                )
                message = '{"v":1,"policy":"answer","content_shape":"text","reason_code":"content_only"}\n' + body
            elif purpose == "summarize_episode":
                payload = json.loads(request.messages[1]["content"])
                user_id, assistant_id = [
                    event["message_id"]
                    for turn in payload["turns"] for event in turn["events"]
                    if event["message_id"]
                ]
                message = json.dumps({
                    "synopsis": [{"text": "助手提议每天90分钟但用户未确认", "source_message_ids": [assistant_id]}],
                    "topics": [],
                    "decisions": [{"text": "用户确认每天60分钟", "source_message_ids": [user_id]}],
                    "outcomes": [], "open_loops": [], "sensitivity": "normal",
                }, ensure_ascii=False)
            elif purpose == "classify_context_dependency":
                message = json.dumps({"independent": "哈希表" in request.messages[-1]["content"]})
            elif purpose == "answer_without_history":
                message = "哈希表通过哈希函数将键映射到存储位置。"
            else:
                raise AssertionError(f"unexpected purpose: {purpose}")
            callback = kwargs.get("on_text_delta")
            if callback is not None:
                callback(message)
            self.model_attempts.append({"purpose": purpose, "status": "succeeded"})
            return ModelResponse(
                message, [], "stop", UsageBuckets(10, 0, 0, 5, 0),
                Timing(started, started, time.perf_counter()), 1,
            )

    monkeypatch.setattr(live, "OpenAICompatibleEmbeddingClient", FakeEmbeddingClient)
    profile = ModelProfile(
        "https://example.invalid/v1", "deepseek-v4-flash", "AGENT_MODEL_API_KEY",
        timeout_seconds=1, max_attempts=3, network_retries=2,
        provider_name="deepseek", context_window=32768, max_output_tokens=8192,
    )
    budget = FakeBudget()
    result = await live.run_round(
        live.Path(__file__).resolve().parents[2], str(migrated_postgres_url),
        tmp_path / "m1-live-offline", 1, profile, budget,
    )

    assert result["dependent_terminal_status"] == "FAILED"
    assert len(result["model_invocations"]) == 6
    assert len(budget.model_attempts) == 6
    assert len(budget.embedding_requests) == 2
