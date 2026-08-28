import json
import io
import socket
import zipfile

import pytest


def test_connector_registration_rejects_unsafe_urls_and_secret_values(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.trusted_connectors import ConnectorSecurityError, TrustedConnectorService

    service = TrustedConnectorService(Database(tmp_path / "agent.db"))
    for url in ("http://example.com", "https://127.0.0.1", "https://localhost", "https://user@example.com", "https://example.com:8443"):
        with pytest.raises(ConnectorSecurityError):
            service.register("bad", url, ["GET"], ["/v1/items"], None, idempotency_key=url)

    monkeypatch.setenv("CONNECTOR_SECRET", "secret-value")
    item = service.register("good", "https://api.example.com", ["GET"], ["/v1/items"], "CONNECTOR_SECRET", idempotency_key="good")
    assert item["credential_env_ref"] == "CONNECTOR_SECRET"
    assert "secret-value" not in json.dumps(item)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.1.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1"])
def test_connector_rejects_any_non_public_dns_answer(tmp_path, address) -> None:
    from app.db import Database
    from app.trusted_connectors import ConnectorSecurityError, TrustedConnectorService

    service = TrustedConnectorService(Database(tmp_path / "agent.db"), resolver=lambda _: [address])
    item = service.register("search", "https://api.example.com", ["GET"], ["/search"], None, idempotency_key="register")
    with pytest.raises(ConnectorSecurityError, match="public"):
        service.execute(item["version_id"], "GET", "/search", query={"q": "x"})


def test_connector_pins_validated_ip_and_checks_peer(tmp_path) -> None:
    from app.db import Database
    from app.trusted_connectors import ConnectorSecurityError, TrustedConnectorService

    calls = []
    def requester(host, ip, method, path, headers, body, timeout, max_bytes):
        calls.append((host, ip, method, path, headers, body, timeout, max_bytes))
        return 200, {"content-type": "application/json"}, b'{"ok":true}', ip

    service = TrustedConnectorService(Database(tmp_path / "agent.db"), resolver=lambda _: ["93.184.216.34"], requester=requester)
    item = service.register("search", "https://api.example.com", ["GET"], ["/search"], None, idempotency_key="register")
    result = service.execute(item["version_id"], "GET", "/search", query={"q": "hello world"})
    assert result["body"] == {"ok": True}
    assert calls[0][0:3] == ("api.example.com", "93.184.216.34", "GET")
    assert calls[0][3] == "/search?q=hello+world"

    bad_peer = TrustedConnectorService(
        Database(tmp_path / "bad.db"), resolver=lambda _: ["93.184.216.34"],
        requester=lambda *args: (200, {}, b"{}", "10.0.0.1"),
    )
    other = bad_peer.register("search", "https://api.example.com", ["GET"], ["/search"], None, idempotency_key="register")
    with pytest.raises(ConnectorSecurityError, match="peer"):
        bad_peer.execute(other["version_id"], "GET", "/search")


def test_connector_rejects_redirect_large_response_and_write_retry(tmp_path) -> None:
    from app.db import Database
    from app.trusted_connectors import ConnectorReconciliationRequired, ConnectorSecurityError, TrustedConnectorService

    db = Database(tmp_path / "agent.db")
    service = TrustedConnectorService(db, resolver=lambda _: ["93.184.216.34"], requester=lambda *args: (302, {"location": "https://other.example"}, b"", "93.184.216.34"))
    item = service.register("write", "https://api.example.com", ["POST"], ["/items"], None, idempotency_key="register")
    with pytest.raises(ConnectorSecurityError, match="redirect"):
        service.execute(item["version_id"], "POST", "/items", body={"x": 1})

    oversized = TrustedConnectorService(db, resolver=lambda _: ["93.184.216.34"], requester=lambda *args: (200, {}, b"x" * 10, "93.184.216.34"))
    with pytest.raises(ConnectorSecurityError, match="response"):
        oversized.execute(item["version_id"], "POST", "/items", body={}, max_response_bytes=5)

    unknown = TrustedConnectorService(db, resolver=lambda _: ["93.184.216.34"], requester=lambda *args: (_ for _ in ()).throw(socket.timeout()))
    with pytest.raises(ConnectorReconciliationRequired):
        unknown.execute(item["version_id"], "POST", "/items", body={})


def test_connector_method_and_path_are_exactly_allowlisted(tmp_path) -> None:
    from app.db import Database
    from app.trusted_connectors import ConnectorSecurityError, TrustedConnectorService

    service = TrustedConnectorService(Database(tmp_path / "agent.db"), resolver=lambda _: ["93.184.216.34"], requester=lambda *args: (200, {}, b"{}", "93.184.216.34"))
    item = service.register("read", "https://api.example.com", ["GET"], ["/v1/items"], None, idempotency_key="register")
    with pytest.raises(ConnectorSecurityError, match="method"):
        service.execute(item["version_id"], "POST", "/v1/items")
    with pytest.raises(ConnectorSecurityError, match="path"):
        service.execute(item["version_id"], "GET", "/v1/items/../secret")


def _connector_skill(connectors: list[str]) -> bytes:
    manifest = {
        "schema_version": 1,
        "name": "connector-skill",
        "version": "1.0.0",
        "title": "连接器技能",
        "description": "调用可信连接器",
        "requested_tools": ["trusted_connector"],
        "connectors": connectors,
        "phases": ["executor"],
        "entry_document": "SKILL.md",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("SKILL.md", "# Connector")
    return buffer.getvalue()


def test_connector_tool_requires_skill_manifest_and_exact_write_approval(tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalRequired, ApprovalService
    from app.skill_platform import SkillPlatform
    from app.tools import ToolCall, ToolRejected, create_default_registry
    from app.trusted_connectors import TrustedConnectorService

    db = Database(tmp_path / "agent.db")
    connector = TrustedConnectorService(
        db,
        resolver=lambda _: ["93.184.216.34"],
        requester=lambda *args: (200, {"content-type": "application/json"}, b'{"id":"1"}', "93.184.216.34"),
    )
    version = connector.register("tasks", "https://api.example.com", ["POST"], ["/tasks"], None, idempotency_key="connector")
    platform = SkillPlatform(db, tmp_path / "skills")
    preview = platform.preview_install(_connector_skill([]))
    denied = platform.confirm_install(preview["install_token"], granted_tools=["trusted_connector"], idempotency_key="denied")
    platform.bind("RUN", "run-denied", [denied["version_id"]], idempotency_key="bind-denied")
    assert platform.tool_authorization(
        "RUN", "run-denied", "trusted_connector", connector_version_id=version["version_id"],
        global_tools={"trusted_connector"}, role_tools={"trusted_connector"}, phase_tools={"trusted_connector"},
        routing_policy_digest="policy-1",
    ) is None

    manifest = json.loads(zipfile.ZipFile(io.BytesIO(_connector_skill(["tasks"]))).read("skill.json"))
    manifest["version"] = "2.0.0"
    package = _connector_skill(["tasks"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("skill.json", json.dumps({**manifest, "version": "2.0.0"}, ensure_ascii=False))
        archive.writestr("SKILL.md", "# Connector v2")
    preview = platform.preview_install(buffer.getvalue())
    installed = platform.confirm_install(preview["install_token"], granted_tools=["trusted_connector"], idempotency_key="allowed")
    platform.bind("RUN", "run-allowed", [installed["version_id"]], idempotency_key="bind-allowed")
    auth = platform.tool_authorization(
        "RUN", "run-allowed", "trusted_connector", connector_version_id=version["version_id"],
        global_tools={"trusted_connector"}, role_tools={"trusted_connector"}, phase_tools={"trusted_connector"},
        routing_policy_digest="policy-1",
    )
    assert auth and auth["skill_version_id"] == installed["version_id"]

    approvals = ApprovalService(db)
    registry = create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals, connectors=connector)
    call = ToolCall("connector-write", "trusted_connector", {
        "connector_version_id": version["version_id"], "method": "POST", "path": "/tasks", "body": {"title": "ride"},
    })
    with pytest.raises(ApprovalRequired):
        registry.execute(call, run_id="run-allowed", skill_tools={"trusted_connector"}, authorization=auth)
    approval = approvals.request("run-allowed", call.id, call.name, call.params, binding=auth)
    approvals.grant(approval.id, "run-allowed", call.id, call.params, binding=auth)
    result = registry.execute(call, run_id="run-allowed", skill_tools={"trusted_connector"}, authorization=auth)
    assert result.ok and result.data["untrusted_external_data"] is True

    changed = {**auth, "routing_policy_digest": "policy-2"}
    replay = ToolCall("connector-write-2", call.name, call.params)
    with pytest.raises(ApprovalRequired):
        registry.execute(replay, run_id="run-allowed", skill_tools={"trusted_connector"}, authorization=changed)


def test_unknown_connector_write_is_reconciliation_required_and_never_replayed(tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalService
    from app.tools import ToolCall, ToolRejected, create_default_registry
    from app.trusted_connectors import TrustedConnectorService

    calls = 0
    def unknown(*args):
        nonlocal calls
        calls += 1
        raise socket.timeout()

    db = Database(tmp_path / "agent.db")
    service = TrustedConnectorService(db, resolver=lambda _: ["93.184.216.34"], requester=unknown)
    item = service.register("tasks", "https://api.example.com", ["POST"], ["/tasks"], None, idempotency_key="connector")
    approvals = ApprovalService(db)
    registry = create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals, connectors=service)
    call = ToolCall("unknown-write", "trusted_connector", {
        "connector_version_id": item["version_id"], "method": "POST", "path": "/tasks", "body": {},
    })
    auth = {
        "skill_version_id": "sv", "package_digest": "package", "grant_snapshot_digest": "grant",
        "routing_policy_digest": "policy", "binding_snapshot_digest": "binding",
    }
    approval = approvals.request("run", call.id, call.name, call.params, binding=auth)
    approvals.grant(approval.id, "run", call.id, call.params, binding=auth)
    with pytest.raises(ToolRejected, match="reconciliation"):
        registry.execute(call, run_id="run", skill_tools={"trusted_connector"}, authorization=auth)
    with pytest.raises(ToolRejected, match="reconciliation"):
        registry.execute(call, run_id="run", skill_tools={"trusted_connector"}, authorization=auth)
    assert calls == 1
    with db.connection() as connection:
        status = connection.execute(
            "SELECT status FROM tool_execution_claims WHERE logical_action_key='run:unknown-write'"
        ).fetchone()[0]
    assert status == "RECONCILIATION_REQUIRED"
