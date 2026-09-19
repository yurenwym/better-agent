import asyncio

from fastapi.testclient import TestClient

from test_runtime import make_runtime


def _headers(app):
    return {
        "host": "127.0.0.1:8000",
        "origin": "http://127.0.0.1:8000",
        "content-type": "application/json",
        "x-csrf-token": app.state.csrf_token,
    }


def test_skills_api_lists_installed_skills(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    response = TestClient(create_app(runtime=runtime)).get(
        "/api/skills",
        headers={"host": "127.0.0.1:8000"},
    )

    assert response.status_code == 200
    skills = response.json()["skills"]
    assert {skill["name"] for skill in skills} >= {"goal-planning", "reflection"}
    assert all(skill["title"] and skill["description"] for skill in skills)


def test_message_rejects_unknown_skill_before_running_model(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = asyncio.run(runtime.create_goal("Goal", "Description"))
    app = create_app(runtime=runtime)
    response = TestClient(app).post(
        f"/api/goals/{run.goal_id}/messages",
        json={"content": "Start", "skill_names": ["not-installed"]},
        headers=_headers(app),
    )

    assert response.status_code == 422
    assert "unknown skill" in response.json()["detail"]


def test_selected_skills_are_persisted_and_loaded_into_model_context(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway

    class ContextModel(MockModelGateway):
        def __init__(self):
            super().__init__()
            self.snapshots = []

        def set_context_snapshot(self, snapshot_hash, text):
            self.snapshots.append(text)

    model = ContextModel()
    runtime = make_runtime(tmp_path, model)
    run = asyncio.run(runtime.create_goal("Goal", "Description"))
    app = create_app(runtime=runtime)
    response = TestClient(app).post(
        f"/api/goals/{run.goal_id}/messages",
        json={"content": "Start", "skill_names": ["reflection"]},
        headers=_headers(app),
    )

    assert response.status_code == 200
    assert runtime.get_run(run.id).skill_names == ("reflection",)
    assert any("执行复盘" in snapshot for snapshot in model.snapshots)
    assert any(event.type == "skills.selected" for event in runtime.events.list(run.id))


def test_selected_skill_tools_are_intersected_with_the_runtime_allowlist(tmp_path) -> None:
    from app.runtime import MockModelGateway

    runtime = make_runtime(tmp_path, MockModelGateway())
    run = asyncio.run(runtime.create_goal("Goal", "Description"))
    selected = asyncio.run(runtime.handle_message(run.id, "Start", ["reflection"]))

    assert runtime._skill_tools_for_run(selected, "react") == set()
