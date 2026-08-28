import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient


def _skill_zip(manifest=None, document="# Travel\n\n## Purpose\nCreate travel plans.", extras=None) -> bytes:
    value = manifest or {
        "schema_version": 1, "name": "travel-planner", "version": "1.0.0", "title": "旅行计划",
        "description": "创建旅行计划", "requested_tools": ["calculator"], "connectors": [],
        "phases": ["conversation", "planner"], "entry_document": "SKILL.md",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.json", json.dumps(value, ensure_ascii=False))
        archive.writestr("SKILL.md", document)
        for name, content in extras or []:
            archive.writestr(name, content)
    return buffer.getvalue()


def test_migration_11_creates_skill_lifecycle_tables(tmp_path) -> None:
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    with db.connection() as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 11 in versions
    assert {"skills", "skill_versions", "skill_grants", "skill_bindings", "trusted_connectors", "trusted_connector_versions", "skill_events"} <= tables


def test_install_preview_confirm_and_same_version_tamper_detection(tmp_path) -> None:
    from app.db import Database
    from app.skill_platform import SkillPlatform, SkillValidationError

    platform = SkillPlatform(Database(tmp_path / "agent.db"), tmp_path / "skills")
    preview = platform.preview_install(_skill_zip())
    assert preview["name"] == "travel-planner"
    assert preview["requested_tools"] == ["calculator"]
    assert not list((tmp_path / "skills").glob("travel-planner/*"))

    installed = platform.confirm_install(preview["install_token"], granted_tools=["calculator"], idempotency_key="install-1")
    assert installed["status"] == "ENABLED"
    assert platform.list()[0]["package_digest"] == preview["package_digest"]
    assert platform.confirm_install(preview["install_token"], granted_tools=["calculator"], idempotency_key="install-1") == installed

    with pytest.raises(SkillValidationError, match="different content"):
        platform.preview_install(_skill_zip(document="# Changed"))


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "C:/windows.txt"])
def test_skill_zip_rejects_path_escape(tmp_path, name) -> None:
    from app.db import Database
    from app.skill_platform import SkillPlatform, SkillValidationError

    platform = SkillPlatform(Database(tmp_path / "agent.db"), tmp_path / "skills")
    with pytest.raises(SkillValidationError, match="path"):
        platform.preview_install(_skill_zip(extras=[(name, "bad")]))


def test_skill_zip_rejects_file_count_and_uncompressed_size_limits(tmp_path) -> None:
    from app.db import Database
    from app.skill_platform import SkillPlatform, SkillValidationError

    platform = SkillPlatform(Database(tmp_path / "agent.db"), tmp_path / "skills")
    with pytest.raises(SkillValidationError, match="files"):
        platform.preview_install(_skill_zip(extras=[(f"assets/{index}.txt", "x") for index in range(199)]))
    with pytest.raises(SkillValidationError, match="file size"):
        platform.preview_install(_skill_zip(extras=[("assets/large.txt", b"x" * (2 * 1024 * 1024 + 1))]))


def test_five_layer_permission_intersection_fails_closed(tmp_path) -> None:
    from app.db import Database
    from app.skill_platform import SkillPlatform

    platform = SkillPlatform(Database(tmp_path / "agent.db"), tmp_path / "skills")
    preview = platform.preview_install(_skill_zip())
    installed = platform.confirm_install(preview["install_token"], granted_tools=["calculator"], idempotency_key="install")

    assert platform.effective_tools(
        installed["version_id"], global_tools={"calculator", "read_note"}, role_tools={"calculator"}, phase_tools={"calculator"},
        phase_name="planner",
    ) == {"calculator"}
    assert platform.effective_tools(installed["version_id"], global_tools=None, role_tools={"calculator"}, phase_tools={"calculator"}, phase_name="planner") == set()
    assert platform.effective_tools(installed["version_id"], global_tools={"calculator"}, role_tools=None, phase_tools={"calculator"}, phase_name="planner") == set()
    assert platform.effective_tools(
        installed["version_id"], global_tools={"calculator"}, role_tools={"calculator"},
        phase_tools={"calculator"}, phase_name="executor",
    ) == set()


def test_binding_freezes_grant_snapshot_for_existing_run(tmp_path) -> None:
    from app.db import Database
    from app.skill_platform import SkillPlatform

    platform = SkillPlatform(Database(tmp_path / "agent.db"), tmp_path / "skills")
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(_skill_zip())).read("skill.json"))
    manifest["requested_tools"] = ["calculator", "read_note"]
    installed = platform.confirm_install(
        platform.preview_install(_skill_zip(manifest))["install_token"],
        granted_tools=["calculator"], idempotency_key="install",
    )
    platform.bind("RUN", "run-1", [installed["version_id"]], idempotency_key="bind")

    platform.grant(installed["version_id"], ["calculator", "read_note"], idempotency_key="expand")

    binding = platform.binding("RUN", "run-1")
    snapshot = binding["grant_snapshots"][installed["version_id"]]
    assert snapshot["granted_tools"] == ["calculator"]
    assert platform.effective_tools(
        installed["version_id"], global_tools={"calculator", "read_note"},
        role_tools={"calculator", "read_note"}, phase_tools={"calculator", "read_note"},
        phase_name="planner", grant_snapshot=snapshot,
    ) == {"calculator"}


def test_binding_pins_version_and_uninstall_keeps_tombstone(tmp_path) -> None:
    from app.db import Database
    from app.skill_platform import SkillPlatform

    platform = SkillPlatform(Database(tmp_path / "agent.db"), tmp_path / "skills")
    first = platform.preview_install(_skill_zip())
    v1 = platform.confirm_install(first["install_token"], granted_tools=["calculator"], idempotency_key="v1")
    binding = platform.bind("THREAD", "thread-1", [v1["version_id"]], idempotency_key="bind-v1")

    manifest_v2 = {**json.loads(zipfile.ZipFile(io.BytesIO(_skill_zip())).read("skill.json")), "version": "2.0.0"}
    second = platform.preview_install(_skill_zip(manifest_v2, document="# Travel v2"))
    v2 = platform.confirm_install(second["install_token"], granted_tools=[], idempotency_key="v2")
    assert platform.binding("THREAD", "thread-1")["version_ids"] == [v1["version_id"]]

    platform.uninstall(v1["skill_id"], idempotency_key="remove")
    assert platform.version(v1["version_id"])["status"] == "UNINSTALLED"
    assert platform.version(v2["version_id"])["status"] == "UNINSTALLED"


def test_skill_install_bind_and_uninstall_api(tmp_path) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway())
    app = create_app(runtime=runtime)
    client = TestClient(app)
    preview = client.post(
        "/api/skills/install", content=_skill_zip(),
        headers={"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/zip", "x-csrf-token": app.state.csrf_token},
    )
    assert preview.status_code == 200
    confirmed = client.post(
        f"/api/skills/{preview.json()['install_token']}/confirm-install", json={"granted_tools": ["calculator"]},
        headers={"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json", "x-csrf-token": app.state.csrf_token, "idempotency-key": "api-install"},
    )
    assert confirmed.status_code == 200
    thread = runtime.conversation.create_thread("Skill")
    bound = client.put(
        f"/api/threads/{thread.id}/skills", json={"version_ids": [confirmed.json()["version_id"]]},
        headers={"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json", "x-csrf-token": app.state.csrf_token, "idempotency-key": "api-bind"},
    )
    assert bound.json()["version_ids"] == [confirmed.json()["version_id"]]
    removed = client.delete(
        f"/api/skills/{confirmed.json()['skill_id']}",
        headers={"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json", "x-csrf-token": app.state.csrf_token, "idempotency-key": "api-remove"},
    )
    assert removed.status_code == 204
    assert all(item["name"] != "travel-planner" for item in client.get("/api/skills", headers={"host": "127.0.0.1:8000"}).json()["skills"])


def test_skill_lifecycle_and_connector_admin_api(tmp_path, monkeypatch) -> None:
    from app.main import create_app
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    runtime = make_runtime(tmp_path, MockModelGateway()); app = create_app(runtime=runtime); client = TestClient(app)
    local = {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000", "content-type": "application/json", "x-csrf-token": app.state.csrf_token}
    preview = client.post("/api/skills/install", content=_skill_zip(), headers={**local, "content-type": "application/zip"}).json()
    installed = client.post(f"/api/skills/{preview['install_token']}/confirm-install", json={"granted_tools": ["calculator"]}, headers={**local, "idempotency-key": "install"}).json()
    versions = client.get(f"/api/skills/{installed['skill_id']}/versions", headers={"host": "127.0.0.1:8000"})
    assert versions.json()["versions"][0]["version_id"] == installed["version_id"]
    disabled = client.post(f"/api/skill-versions/{installed['version_id']}/disable", json={}, headers={**local, "idempotency-key": "disable"})
    assert disabled.json()["status"] == "DISABLED"
    conversation_skills = client.get("/api/skills", headers={"host": "127.0.0.1:8000"}).json()["skills"]
    assert all(item["name"] != "travel-planner" for item in conversation_skills)
    installed_skills = client.get("/api/skills?include_disabled=true", headers={"host": "127.0.0.1:8000"}).json()["skills"]
    managed = next(item for item in installed_skills if item["name"] == "travel-planner")
    assert managed["enabled"] is False
    enabled = client.post(f"/api/skill-versions/{installed['version_id']}/enable", json={}, headers={**local, "idempotency-key": "enable"})
    assert enabled.json()["status"] == "ENABLED"
    grant = client.put(f"/api/skill-versions/{installed['version_id']}/grant", json={"granted_tools": []}, headers={**local, "idempotency-key": "grant"})
    assert grant.json()["granted_tools"] == []

    monkeypatch.setenv("CONNECTOR_API_KEY", "secret-value")
    runtime.connectors.resolver = lambda _: ["93.184.216.34"]
    connector = client.post("/api/trusted-connectors", json={
        "name": "search", "base_url": "https://api.example.com", "methods": ["GET"], "paths": ["/search"],
        "credential_env_ref": "CONNECTOR_API_KEY", "request_schema": {"type": "object"},
    }, headers={**local, "idempotency-key": "connector"})
    assert connector.status_code == 201 and "secret-value" not in connector.text
    verified = client.post(f"/api/trusted-connector-versions/{connector.json()['version_id']}/verify", json={}, headers={**local, "idempotency-key": "verify"})
    assert verified.status_code == 200 and verified.json()["verified_addresses"] == 1
    listed = client.get("/api/trusted-connectors", headers={"host": "127.0.0.1:8000"})
    assert listed.json()["connectors"][0]["request_schema"] == {"type": "object"}


@pytest.mark.asyncio
async def test_conversation_pins_skill_version_and_applies_its_snapshot_after_update(tmp_path) -> None:
    from app.runtime import MockModelGateway
    from test_runtime import make_runtime

    class CapturingConversationModel:
        def __init__(self) -> None:
            self.history = []

        async def route_and_respond(self, *, history, on_text_delta, **_kwargs):
            self.history = history
            response = '{"v":1,"policy":"answer","content_shape":"text","reason_code":"content_only"}\n完成'
            on_text_delta(response)
            return type("Response", (), {"message": response, "tool_calls": []})()

    runtime = make_runtime(tmp_path, MockModelGateway())
    model = CapturingConversationModel()
    runtime.conversation.route_model = model
    v1_preview = runtime.skill_platform.preview_install(_skill_zip(document="# Travel v1\n\n## Purpose\nV1 only"))
    v1 = runtime.skill_platform.confirm_install(v1_preview["install_token"], granted_tools=[], idempotency_key="v1")
    thread = runtime.conversation.create_thread("Skill pin")
    submitted = runtime.conversation.accept_turn(thread.id, "client-1", "help", ["travel-planner"])

    manifest_v2 = {**json.loads(zipfile.ZipFile(io.BytesIO(_skill_zip())).read("skill.json")), "version": "2.0.0"}
    v2_preview = runtime.skill_platform.preview_install(_skill_zip(manifest_v2, document="# Travel v2\n\n## Purpose\nV2 only"))
    runtime.skill_platform.confirm_install(v2_preview["install_token"], granted_tools=[], idempotency_key="v2")

    assert await runtime.turn_worker.run_once() is True
    binding = runtime.skill_platform.binding("THREAD", thread.id)
    assert binding["version_ids"] == [v1["version_id"]]
    assert "V1 only" in model.history[0]["content"]
    assert "V2 only" not in model.history[0]["content"]
    events = runtime.conversation.events.list(thread.id)
    snapshot = next(event for event in events if event.type == "skill.snapshot_applied")
    assert snapshot.turn_id == submitted.turn_id
    assert snapshot.data["skill_version_ids"] == [v1["version_id"]]
