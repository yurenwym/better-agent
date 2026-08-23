from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import AppConfig
from app.db import Database
from app.events import EventStore
from app.main import create_app
from app.research.service import ResearchService
from app.runtime import AgentRuntime, MockModelGateway
from app.domain import ApprovalService, CheckpointStore, PlanVersionService
from app.memory import MemoryService
from app.tools import create_default_registry


def client(tmp_path):
    db = Database(tmp_path / "agent.db")
    events = EventStore(db); approvals = ApprovalService(db)
    runtime = AgentRuntime(db=db, events=events, plans=PlanVersionService(db), approvals=approvals,
        checkpoints=CheckpointStore(db), memory=MemoryService(db, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "artifacts", db=db, approval_service=approvals), model=MockModelGateway())
    runtime.research = ResearchService(db, runtime.conversation.events)
    app = create_app(AppConfig(), runtime=runtime)
    return TestClient(app, base_url="http://localhost"), runtime


def test_research_routes_create_list_detail_cancel_retry(tmp_path) -> None:
    http, runtime = client(tmp_path)
    csrf = http.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    thread = http.post("/api/threads", headers=headers, json={}).json()
    created = http.post(f"/api/threads/{thread['id']}/research", headers=headers, json={"client_request_id":"r1","topic":"研究 SQLite","source_scopes":["web"]})
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    assert http.get(f"/api/research/jobs/{job_id}").json()["topic"] == "研究 SQLite"
    assert http.get(f"/api/research/jobs?thread_id={thread['id']}").json()["jobs"][0]["id"] == job_id
    assert http.post(f"/api/research/jobs/{job_id}/cancel", headers=headers, json={}).json()["status"] == "CANCELLED"
    retried = http.post(f"/api/research/jobs/{job_id}/retry", headers=headers, json={"client_request_id":"retry1"})
    assert retried.status_code == 202 and retried.json()["job_id"] != job_id


def test_research_job_api_returns_a_stable_failure_reason(tmp_path) -> None:
    http, runtime = client(tmp_path)
    csrf = http.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    thread = http.post("/api/threads", headers=headers, json={}).json()
    job = runtime.research.create_manual(thread["id"], "research", "failed-api", ("web",))
    runtime.research.claim_next("worker", 30)
    runtime.research.fail(job.id, "worker", "unknowncitation")
    payload = http.get(f"/api/research/jobs/{job.id}").json()
    assert payload["failure_reason_code"] == "unknowncitation"
