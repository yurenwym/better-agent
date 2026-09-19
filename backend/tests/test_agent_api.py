from fastapi.testclient import TestClient
import time

from app.main import create_app
from app.startup import build_runtime


def headers(app):
    return {"host":"127.0.0.1:8000","origin":"http://127.0.0.1:8000","content-type":"application/json","x-csrf-token":app.state.csrf_token}


def test_expert_run_api_create_list_cancel_and_stream(tmp_path):
    runtime = build_runtime(tmp_path)
    app = create_app(runtime=runtime)
    http = TestClient(app)
    thread = http.post("/api/threads", headers=headers(app), json={"title":"专家测试"}).json()
    created = http.post(f"/api/threads/{thread['id']}/expert-runs", headers=headers(app), json={"objective":"比较三个方案","idempotency_key":"expert-api"})
    assert created.status_code == 202
    run = created.json()
    assert run["status"] == "QUEUED" and run["runtime_bundle_id"]
    local = {"host":"127.0.0.1:8000"}
    assert http.get(f"/api/agent-runs/{run['id']}/tasks", headers=local).json()["tasks"]
    stream = http.get(f"/api/agent-runs/{run['id']}/events/stream?follow=0", headers=local)
    assert "agent.run.created" in stream.text
    cancelled = http.post(f"/api/agent-runs/{run['id']}/cancel", headers=headers(app), json={"reason":"停止"})
    assert cancelled.json()["status"] == "CANCELLED"


def test_expert_run_rejects_unknown_thread_and_empty_objective(tmp_path):
    runtime = build_runtime(tmp_path); app = create_app(runtime=runtime); http = TestClient(app)
    assert http.post("/api/threads/missing/expert-runs", headers=headers(app), json={"objective":"x","idempotency_key":"x"}).status_code == 404
    thread = http.post("/api/threads", headers=headers(app), json={}).json()
    assert http.post(f"/api/threads/{thread['id']}/expert-runs", headers=headers(app), json={"objective":"","idempotency_key":"x"}).status_code == 422


def test_expert_run_worker_without_model_fails_visibly(tmp_path, monkeypatch):
    for name in (
        "LLM_AP_PATH",
        "AGENT_MODEL_BASE_URL",
        "AGENT_MODEL_ID",
        "AGENT_MODEL_API_KEY",
        "AGENT_FALLBACK_MODEL_BASE_URL",
        "AGENT_FALLBACK_MODEL_ID",
        "AGENT_FALLBACK_MODEL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    runtime = build_runtime(tmp_path)
    app = create_app(runtime=runtime)
    with TestClient(app) as http:
        thread = http.post("/api/threads", headers=headers(app), json={"title":"纵向闭环"}).json()
        created = http.post(
            f"/api/threads/{thread['id']}/expert-runs", headers=headers(app),
            json={"objective":"比较三个方案","idempotency_key":"expert-e2e"},
        ).json()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = http.get(f"/api/agent-runs/{created['id']}", headers={"host":"127.0.0.1:8000"}).json()
            if run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
            time.sleep(.02)
        assert run["status"] == "FAILED"
        tasks = http.get(
            f"/api/agent-runs/{created['id']}/tasks", headers={"host":"127.0.0.1:8000"}
        ).json()["tasks"]
        assert all(item["result_artifact_id"] is None for item in tasks)
        assert any(item["error_code"] == "ALL_EXPERTS_FAILED" for item in tasks)
        messages = http.get(
            f"/api/threads/{thread['id']}/messages", headers={"host":"127.0.0.1:8000"}
        ).json()["messages"]
        assistant = [item for item in messages if item["role"] == "assistant"]
        assert assistant == []


def test_expert_run_is_visible_as_a_user_message_while_processing(tmp_path):
    runtime = build_runtime(tmp_path)
    app = create_app(runtime=runtime)
    http = TestClient(app)
    thread = http.post("/api/threads", headers=headers(app), json={"title":"可见请求"}).json()
    response = http.post(
        f"/api/threads/{thread['id']}/expert-runs", headers=headers(app),
        json={"objective":"分析这个目标","idempotency_key":"visible-objective"},
    )
    assert response.status_code == 202
    messages = http.get(
        f"/api/threads/{thread['id']}/messages", headers={"host":"127.0.0.1:8000"}
    ).json()["messages"]
    assert [(item["role"], item["content"]) for item in messages] == [("user", "分析这个目标")]


def test_thread_can_restore_its_latest_expert_run(tmp_path):
    runtime = build_runtime(tmp_path)
    app = create_app(runtime=runtime)
    http = TestClient(app)
    thread = http.post("/api/threads", headers=headers(app), json={"title":"恢复专家"}).json()
    first = http.post(
        f"/api/threads/{thread['id']}/expert-runs", headers=headers(app),
        json={"objective":"第一个","idempotency_key":"restore-first"},
    ).json()
    latest = http.get(
        f"/api/threads/{thread['id']}/expert-runs/latest", headers={"host":"127.0.0.1:8000"}
    )
    assert latest.status_code == 200 and latest.json()["id"] == first["id"]

    empty = http.post("/api/threads", headers=headers(app), json={"title":"没有专家"}).json()
    assert http.get(
        f"/api/threads/{empty['id']}/expert-runs/latest", headers={"host":"127.0.0.1:8000"}
    ).status_code == 404
