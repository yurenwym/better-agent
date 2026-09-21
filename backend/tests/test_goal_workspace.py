from datetime import date, timedelta

import pytest

from test_goal_program_api import headers, setup_app


def test_saved_document_is_delivered_without_a_pending_execution_step(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    body = client.get(f"/api/workspaces/{version.plan_document_id}", headers={"host": "127.0.0.1:8000"}).json()

    # A delivered document is a complete outcome: the primary next action is
    # viewing or editing it, and execution management is optional.
    assert body["phase"] == "DELIVERED"
    assert body["program"] is None
    assert body["next_action"]["kind"] == "open_plan"
    assert body["next_action"]["href"] == f"/plans/{version.plan_document_id}"


def test_workspace_projects_plan_execution_review_and_memory_into_one_next_action(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    response = client.get("/api/workspaces/missing", headers={"host": "127.0.0.1:8000"})
    assert response.status_code == 404

    payload = {"start_date": "2026-09-01", "requested_end_date": "2026-09-07", "timezone": "Asia/Shanghai", "daily_minutes": 60}
    preview = client.post(f"/api/plans/{version.plan_document_id}/program-preview", json=payload, headers=headers(app, "preview")).json()
    workspace = client.get(f"/api/workspaces/{version.plan_document_id}", headers={"host": "127.0.0.1:8000"})
    assert workspace.status_code == 200
    body = workspace.json()
    assert body["resource_id"] == version.plan_document_id
    assert body["phase"] == "READY_TO_START"
    assert body["next_action"]["kind"] == "activate_program"
    assert len(body["sources"]) >= 1

    active = client.post(f"/api/programs/{preview['id']}/activate", json={"expected_version": preview["version"]}, headers=headers(app, "activate")).json()
    workspace = client.get(f"/api/workspaces/{version.plan_document_id}", headers={"host": "127.0.0.1:8000"}).json()
    assert workspace["phase"] == "EXECUTING"
    assert workspace["next_action"]["kind"] in {"complete_action", "open_today"}
    assert workspace["today"]["program_id"] == active["id"]


def test_workspace_is_deterministic_owner_scoped_and_completed_program_has_no_action(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    payload = {"start_date": "2026-09-01", "requested_end_date": "2026-09-01", "timezone": "Asia/Shanghai", "daily_minutes": 60}
    preview = client.post(f"/api/plans/{version.plan_document_id}/program-preview", json=payload, headers=headers(app, "preview")).json()
    active = client.post(f"/api/programs/{preview['id']}/activate", json={"expected_version": preview["version"]}, headers=headers(app, "activate")).json()
    action = active["actions"][0]
    client.post(f"/api/actions/{action['id']}/complete", json={"expected_version": action["version"]}, headers=headers(app, "complete"))
    current = client.get(f"/api/programs/{active['id']}", headers={"host": "127.0.0.1:8000"}).json()
    client.post(f"/api/programs/{active['id']}/complete", json={"expected_version": current["version"]}, headers=headers(app, "finish"))
    first = client.get(f"/api/workspaces/{version.plan_document_id}", headers={"host": "127.0.0.1:8000"}).json()
    second = client.get(f"/api/workspaces/{version.plan_document_id}", headers={"host": "127.0.0.1:8000"}).json()
    assert first == second
    assert first["phase"] == "COMPLETED"
    assert first["next_action"] is None
    assert first["growth"]["episode_id"].startswith("episode_")

    with pytest.raises(KeyError):
        runtime.goal_programs.get(active["id"], owner_id="other-user")
