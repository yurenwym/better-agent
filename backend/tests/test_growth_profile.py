from test_goal_program_api import headers, setup_app


def test_growth_profile_projects_user_progress_without_sensitive_feedback_text(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    runtime.memory_store = __import__("app.memory_v2", fromlist=["MemoryStore"]).MemoryStore(runtime.db, tmp_path / "memory")
    payload = {"start_date": "2026-09-01", "requested_end_date": "2026-09-01", "timezone": "Asia/Shanghai", "daily_minutes": 60}
    preview = client.post(f"/api/plans/{version.plan_document_id}/program-preview", json=payload, headers=headers(app, "preview")).json()
    active = client.post(f"/api/programs/{preview['id']}/activate", json={"expected_version": preview["version"]}, headers=headers(app, "activate")).json()
    action = active["actions"][0]
    client.post(f"/api/actions/{action['id']}/complete", json={"expected_version": action["version"]}, headers=headers(app, "complete"))
    client.post(
        f"/api/actions/{action['id']}/feedback",
        json={"expected_version": 1, "kind": "completion", "actual_minutes": 75, "difficulty": 4, "note": "private health detail"},
        headers=headers(app, "feedback"),
    )
    current = client.get(f"/api/programs/{active['id']}", headers={"host": "127.0.0.1:8000"}).json()
    client.post(f"/api/programs/{active['id']}/complete", json={"expected_version": current["version"]}, headers=headers(app, "finish"))

    response = client.get("/api/growth/profile", headers={"host": "127.0.0.1:8000"})
    assert response.status_code == 200
    profile = response.json()
    assert profile["owner_id"] == "local-user"
    assert profile["metrics"]["completed_programs"] == 1
    assert profile["metrics"]["completed_actions"] == 1
    assert profile["metrics"]["average_difficulty"] == 4
    assert profile["metrics"]["average_actual_minutes"] == 75
    assert profile["programs"][0]["completion_episode_id"].startswith("episode_")
    assert "private health detail" not in response.text


def test_growth_program_history_is_owner_scoped_and_stable(tmp_path) -> None:
    runtime, app, client, version = setup_app(tmp_path)
    payload = {"start_date": "2026-09-01", "requested_end_date": "2026-09-07", "timezone": "Asia/Shanghai", "daily_minutes": 60}
    preview = client.post(f"/api/plans/{version.plan_document_id}/program-preview", json=payload, headers=headers(app, "preview")).json()
    first = client.get(f"/api/growth/programs/{preview['id']}", headers={"host": "127.0.0.1:8000"})
    second = client.get(f"/api/growth/programs/{preview['id']}", headers={"host": "127.0.0.1:8000"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["program"]["id"] == preview["id"]
    assert client.get("/api/growth/programs/missing", headers={"host": "127.0.0.1:8000"}).status_code == 404
