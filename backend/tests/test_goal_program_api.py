from fastapi.testclient import TestClient

from test_api import _headers
from test_runtime import make_runtime


def setup_app(tmp_path):
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    thread = client.post("/api/threads", json={"title": "LeetCode"}, headers=_headers(app)).json()
    version = runtime.plan_documents.save_model_revision(
        thread_id=thread["id"], title="一周力扣", markdown_content="# 一周力扣\n每天一道题",
        source_turn_id=None, source_message_id=None, actor="user",
    )
    return runtime, app, client, version


def headers(app, key):
    return {**_headers(app), "Idempotency-Key": key}


def test_period_review_retry_endpoint_recovers_without_completing_program_again(tmp_path):
    import asyncio
    from test_goal_period_review import _FailingPeriodCompiler, _PeriodCompiler
    from test_goal_programs import preview

    runtime, app, client, version = setup_app(tmp_path)
    goals = runtime.goal_programs
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    for action in active["actions"]:
        goals.complete_action(action["id"], expected_version=0, idempotency_key=action["id"])
    goals.transition(draft["id"], "complete", expected_version=active["version"], idempotency_key="complete")
    goals.compiler = _FailingPeriodCompiler()
    asyncio.run(goals.period_summary(draft["id"]))
    url = f"/api/programs/{draft['id']}/period-review"
    assert client.get(url, headers=_headers(app)).json()["status"] == "FAILED"
    assert client.post(url + "/retry", json={}, headers=_headers(app)).status_code == 422
    goals.compiler = _PeriodCompiler("Recovered")
    response = client.post(url + "/retry", json={}, headers=headers(app, "retry"))
    assert response.status_code == 200 and response.json()["status"] == "COMPLETED"
    assert client.post(url + "/retry", json={}, headers=headers(app, "retry")).json()["summary"] == "Recovered"
    assert goals.compiler.calls == 1
    assert goals.get(draft["id"])["completion_summary"] == "Recovered"

def test_preview_activate_today_and_mutations_round_trip(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    preview = client.post(
        f"/api/plans/{version.plan_document_id}/program-preview",
        json={"start_date":"2026-09-01","requested_end_date":"2026-09-07","timezone":"Asia/Shanghai","daily_minutes":60},
        headers=headers(app,"preview"),
    )
    assert preview.status_code == 200
    draft = preview.json()
    active = client.post(f"/api/programs/{draft['id']}/activate", json={"expected_version":draft["version"]}, headers=headers(app,"activate"))
    assert active.status_code == 200 and len(active.json()["actions"]) == 7
    action = active.json()["actions"][0]

    completed = client.post(f"/api/actions/{action['id']}/complete", json={"expected_version":0}, headers=headers(app,"complete"))
    feedback = client.post(f"/api/actions/{action['id']}/feedback", json={"expected_version":1,"kind":"difficulty","difficulty":3}, headers=headers(app,"feedback"))
    today = client.get("/api/today?date=2026-09-02", headers={"host":"127.0.0.1:8000"})
    assert completed.status_code == feedback.status_code == today.status_code == 200
    assert today.json()["programs"][0]["today"][0]["scheduled_date"] == "2026-09-02"
    assert client.get(f"/api/programs/{draft['id']}", headers={"host":"127.0.0.1:8000"}).json()["timezone"] == "Asia/Shanghai"


def test_mutations_require_csrf_idempotency_and_report_safe_conflicts(tmp_path) -> None:
    _, app, client, version = setup_app(tmp_path)
    payload={"start_date":"2026-09-01","requested_end_date":"2026-09-07","timezone":"Asia/Shanghai","daily_minutes":60}
    missing_key=client.post(f"/api/plans/{version.plan_document_id}/program-preview",json=payload,headers=_headers(app))
    missing_csrf=client.post(f"/api/plans/{version.plan_document_id}/program-preview",json=payload,headers={"host":"127.0.0.1:8000","content-type":"application/json","Idempotency-Key":"x"})
    draft=client.post(f"/api/plans/{version.plan_document_id}/program-preview",json=payload,headers=headers(app,"preview")).json()
    conflict=client.post(f"/api/programs/{draft['id']}/activate",json={"expected_version":999},headers=headers(app,"conflict"))
    assert missing_key.status_code == 422
    assert missing_csrf.status_code == 403
    assert conflict.status_code == 409
    assert "current" in conflict.json() and "sqlite" not in conflict.text.lower()


def test_lifecycle_and_request_help_routes_do_not_create_run(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    payload={"start_date":"2026-09-01","requested_end_date":"2026-09-07","timezone":"Asia/Shanghai","daily_minutes":60}
    draft=client.post(f"/api/plans/{version.plan_document_id}/program-preview",json=payload,headers=headers(app,"preview")).json()
    active=client.post(f"/api/programs/{draft['id']}/activate",json={"expected_version":draft["version"]},headers=headers(app,"activate")).json()
    help_response=client.post(f"/api/actions/{active['actions'][0]['id']}/request-help",json={"content":"帮我拆解","expected_version":0},headers=headers(app,"help"))
    paused=client.post(f"/api/programs/{draft['id']}/pause",json={"expected_version":active["version"]},headers=headers(app,"pause"))
    assert help_response.status_code == 202 and paused.status_code == 200 and paused.json()["status"] == "PAUSED"
    with runtime.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_completed_program_returns_memory_summary_over_http(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    draft=client.post(f"/api/plans/{version.plan_document_id}/program-preview",headers=headers(app,"preview"),json={"start_date":"2026-09-01","requested_end_date":"2026-09-07","timezone":"Asia/Shanghai","daily_minutes":60}).json()
    active=client.post(f"/api/programs/{draft['id']}/activate",headers=headers(app,"activate"),json={"expected_version":draft["version"]}).json()
    for index,action in enumerate(active["actions"]):
        client.post(f"/api/actions/{action['id']}/complete",headers=headers(app,f"complete-{index}"),json={"expected_version":action["version"]})
    ready=client.get(f"/api/programs/{draft['id']}",headers={"host":"127.0.0.1:8000"}).json()

    completed=client.post(f"/api/programs/{draft['id']}/complete",headers=headers(app,"finish"),json={"expected_version":ready["version"]})

    assert completed.status_code==200
    assert completed.json()["completion_episode_id"].startswith("episode_")
    assert completed.json()["completion_summary"].startswith("已完成目标")


def test_failed_review_retry_requires_mutation_headers_and_is_idempotent(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    runtime.goal_reviews.queue_delay_seconds = 0
    draft = client.post(
        f"/api/plans/{version.plan_document_id}/program-preview",
        headers=headers(app, "preview"),
        json={"start_date":"2026-09-01","requested_end_date":"2026-09-07","timezone":"Asia/Shanghai","daily_minutes":60},
    ).json()
    active = client.post(
        f"/api/programs/{draft['id']}/activate",
        headers=headers(app, "activate"),
        json={"expected_version":draft["version"]},
    ).json()
    action = active["actions"][0]
    client.post(
        f"/api/actions/{action['id']}/complete",
        headers=headers(app, "complete"),
        json={"expected_version":action["version"]},
    )
    closed = client.post(f"/api/programs/{draft['id']}/days/{action['scheduled_date']}/close", headers=headers(app, "close"), json={})
    assert closed.status_code == 200
    review = runtime.goal_reviews.claim_next("broken", 30)
    runtime.goal_reviews.fail(review["id"], "broken", "INVALID_MODEL_OUTPUT")
    review = runtime.goal_reviews.claim_next("broken", 30)
    runtime.goal_reviews.fail(review["id"], "broken", "INVALID_MODEL_OUTPUT")
    with runtime.db.connection() as connection:
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM goal_review_action_snapshots WHERE review_id=?", (review["id"],)
        ).fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM goal_program_events WHERE program_id=? AND type='review.retry_queued'", (draft["id"],)
        ).fetchone()[0]

    missing_key = client.post(f"/api/reviews/{review['id']}/retry", headers=_headers(app))
    missing_csrf = client.post(
        f"/api/reviews/{review['id']}/retry",
        headers={"host":"127.0.0.1:8000","content-type":"application/json","Idempotency-Key":"retry"},
    )
    first = client.post(f"/api/reviews/{review['id']}/retry", headers=headers(app, "retry"))
    claimed = runtime.goal_reviews.claim_next("broken-again", 30)
    runtime.goal_reviews.fail(claimed["id"], "broken-again", "INVALID_MODEL_OUTPUT")
    claimed = runtime.goal_reviews.claim_next("broken-again", 30)
    runtime.goal_reviews.fail(claimed["id"], "broken-again", "INVALID_MODEL_OUTPUT")
    repeated = client.post(f"/api/reviews/{review['id']}/retry", headers=headers(app, "retry"))

    assert missing_key.status_code == 422
    assert missing_csrf.status_code == 403
    assert first.status_code == repeated.status_code == 200
    assert first.json()["status"] == repeated.json()["status"] == "QUEUED"
    with runtime.db.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM goal_review_action_snapshots WHERE review_id=?", (review["id"],)
        ).fetchone()[0] == snapshot_count
        assert connection.execute(
            "SELECT COUNT(*) FROM goal_program_events WHERE program_id=? AND type='review.retry_queued'", (draft["id"],)
        ).fetchone()[0] == event_count + 1
