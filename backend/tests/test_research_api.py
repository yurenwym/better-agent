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


def test_research_routes_do_not_expose_or_mutate_another_owners_job(tmp_path):
    http, runtime = client(tmp_path)
    csrf = http.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf, "x-owner-id": "other", "Content-Type": "application/json"}
    thread = runtime.conversation.create_thread()
    job = runtime.research.create_manual(thread.id, "private", "private", ("web",))
    base = f"/api/research/jobs/{job.id}"
    for suffix in ("", "/report", "/sources"):
        assert http.get(base + suffix, headers=headers).status_code == 404
    assert http.get("/api/research/jobs", headers=headers).json() == {"jobs": []}
    assert http.post(base + "/cancel", headers=headers).status_code == 404
    assert http.post(base + "/retry", headers=headers, json={"client_request_id": "other"}).status_code == 404
    assert http.delete(base, headers=headers).status_code == 404
    assert http.post(f"/api/threads/{thread.id}/research", headers=headers,
                     json={"client_request_id": "other", "topic": "other"}).status_code == 404
    assert runtime.research.get(job.id).status == "QUEUED"
    assert len(runtime.research.list()) == 1


def test_research_job_api_returns_a_stable_failure_reason(tmp_path) -> None:
    http, runtime = client(tmp_path)
    csrf = http.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    thread = http.post("/api/threads", headers=headers, json={}).json()
    job = runtime.research.create_manual(thread["id"], "research", "failed-api", ("web",))
    lease_1 = runtime.research.claim_next("worker", 30)
    runtime.research.fail(job.id, "worker", "unknowncitation", epoch=lease_1.lease_epoch)
    payload = http.get(f"/api/research/jobs/{job.id}").json()
    assert payload["failure_reason_code"] == "unknowncitation"


def test_research_job_api_returns_missing_requirements(tmp_path) -> None:
    http, runtime = client(tmp_path)
    csrf = http.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    thread = http.post("/api/threads", headers=headers, json={}).json()
    job = runtime.research.create_manual(thread["id"], "research", "failed-details-api", ("web",))
    lease_1 = runtime.research.claim_next("worker", 30)
    runtime.research.fail(job.id, "worker", "topiccoverageerror", diagnostics={"missing_requirements":["投递渠道"]}, epoch=lease_1.lease_epoch)

    payload = http.get(f"/api/research/jobs/{job.id}").json()
    assert payload["failure_details"] == {"missing_requirements": ["投递渠道"]}


def test_research_job_api_exposes_partial_traceability(tmp_path) -> None:
    http, runtime = client(tmp_path)
    csrf = http.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    thread = http.post("/api/threads", headers=headers, json={}).json()
    job = runtime.research.create_manual(thread["id"], "research", "partial-api", ("web",))
    lease_1 = runtime.research.claim_next("worker", 30)
    matrix = ({"requirement": "A", "supported": True},)
    runtime.research.complete_partial(job.id, "worker", "partial", "# partial", 1, 1, matrix, ("B",), epoch=lease_1.lease_epoch)

    payload = http.get(f"/api/research/jobs/{job.id}").json()
    assert payload["status"] == "PARTIAL"
    assert payload["missing_requirements"] == ["B"]
    assert payload["traceability"] == [{"requirement": "A", "supported": True}]
    assert http.get(f"/api/research/jobs/{job.id}/report").status_code == 200


def test_research_job_delete_requires_terminal_state_and_removes_it(tmp_path) -> None:
    http, runtime = client(tmp_path)
    csrf = http.get("/api/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf, "Content-Type": "application/json"}
    thread = http.post("/api/threads", headers=headers, json={}).json()
    job = runtime.research.create_manual(thread["id"], "delete me", "delete-api", ("web",))
    active = http.delete(f"/api/research/jobs/{job.id}", headers=headers)
    assert active.status_code == 409
    runtime.research.cancel(job.id)
    deleted = http.delete(f"/api/research/jobs/{job.id}", headers=headers)
    assert deleted.status_code == 204
    assert http.get(f"/api/research/jobs/{job.id}").status_code == 404
