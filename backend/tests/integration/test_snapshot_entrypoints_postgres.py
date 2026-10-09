"""A newly integrated business adapter sees its committed snapshot in PostgreSQL."""
from __future__ import annotations

import json

import pytest

from app.db import Database
from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
from app.model_admin import ModelAdminService
from app.research.live import LiveResearchModel
from app.research.models import ResearchLimits
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_snapshot_gateway import _answer, _configured_control_plane


@pytest.mark.asyncio
async def test_research_plan_is_committed_before_postgres_transport_send(migrated_postgres_url, tmp_path, monkeypatch):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        db, bundle, _ = _configured_control_plane(
            tmp_path, monkeypatch, database=db, owner_id="pg-research-owner",
        )
        response = _answer(json.dumps({"title": "PG", "sections": ["结果", "依据"], "queries": ["PG 结果"]}, ensure_ascii=False))
        observer = CommittedSnapshotTransport(db, "pg-research-owner", "CS-RS-01", response)
        gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=observer)
        context = ModelCallContext(role="researcher", purpose="research_structured_step",
                                   owner_id="pg-research-owner", runtime_bundle_id=bundle.id)
        token = gateway.set_call_context(context)
        try:
            plan = await LiveResearchModel(gateway).plan("PG", ResearchLimits())
        finally:
            gateway.reset_call_context(token)
        assert plan.title == "PG"
        assert len(observer.observations) == 1
        assert observer.observations[0]["snapshot_id"]
        assert observer.observations[0]["attempt_id"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_model_admin_verification_failure_is_bound_and_owner_scoped_in_postgres(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    import httpx
    from app.model_gateway import GatewayError

    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        observations = []

        def fail_after_commit(request: httpx.Request) -> httpx.Response:
            with db.connection() as connection:
                row = connection.execute(
                    "SELECT a.id AS attempt_id,i.id AS invocation_id,i.owner_id,i.purpose,"
                    "i.context_snapshot_id,i.context_snapshot_digest,i.status AS invocation_status "
                    "FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id "
                    "WHERE a.status='STARTED' ORDER BY a.started_at DESC,a.id DESC LIMIT 1",
                ).fetchone()
            assert row is not None
            assert row["owner_id"] == "pg-admin-owner"
            assert row["purpose"] == "verify_model_profile_version"
            assert row["context_snapshot_id"] and row["context_snapshot_digest"]
            assert row["invocation_status"] == "RUNNING"
            assert request.url.path.endswith("/chat/completions")
            observations.append(dict(row))
            raise httpx.ConnectError("provider unavailable", request=request)

        service = ModelAdminService(
            db, owner_id="pg-admin-owner", control_store=ModelControlStore(db),
            verification_transport=httpx.MockTransport(fail_after_commit),
        )
        created = service.create_profile({
            "name": "PG verification", "provider_protocol": "openai_compatible",
            "provider_name": "test", "base_url": "https://provider.test/v1",
            "model_name": "verify-v1", "credential_env_ref": "PG_ADMIN_TEST_KEY",
            "capabilities": {"text": True}, "context_window": 32768,
            "max_output_tokens": 1024, "timeout_seconds": 10, "max_attempts": 2,
        }, validate_capacity=False)
        version_id = created["versions"][0]["id"]
        monkeypatch.setenv("PG_ADMIN_TEST_KEY", "test-secret")

        other = ModelAdminService(db, owner_id="other-pg-owner", control_store=ModelControlStore(db))
        with pytest.raises(KeyError):
            await other.verify(version_id)
        assert observations == []

        with pytest.raises(GatewayError, match="retry budget exhausted"):
            await service.verify(version_id)

        assert len(observations) == 2
        assert len({item["invocation_id"] for item in observations}) == 1
        assert len({item["context_snapshot_id"] for item in observations}) == 1
        assert len({item["attempt_id"] for item in observations}) == 2
        assert service.version(version_id)["verification_status"] == "FAILED"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_model_admin_snapshot_persistence_failure_prevents_postgres_send(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    import httpx
    from app.db import Database
    from app.model_admin import ModelAdminService
    from app.model_control import ModelControlStore

    db = Database(migrated_postgres_url, workspace=tmp_path)
    sends = []

    def unexpected_send(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    try:
        service = ModelAdminService(
            db, owner_id="pg-storage-owner", control_store=ModelControlStore(db),
            verification_transport=httpx.MockTransport(unexpected_send),
        )
        created = service.create_profile({
            "name": "PG persistence failure", "provider_protocol": "openai_compatible",
            "provider_name": "test", "base_url": "https://provider.test/v1",
            "model_name": "verify-v1", "credential_env_ref": "PG_ADMIN_STORAGE_KEY",
            "capabilities": {"text": True}, "context_window": 32768,
            "max_output_tokens": 1024, "timeout_seconds": 10, "max_attempts": 1,
        }, validate_capacity=False)
        version_id = created["versions"][0]["id"]
        monkeypatch.setenv("PG_ADMIN_STORAGE_KEY", "test-secret")
        with db.connection() as connection:
            connection.execute("""
                CREATE FUNCTION reject_snapshot_insert() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'injected snapshot persistence failure'; END;
                $$ LANGUAGE plpgsql
            """)
            connection.execute("""
                CREATE TRIGGER reject_snapshot_insert_before_send
                BEFORE INSERT ON model_input_snapshots
                FOR EACH ROW EXECUTE FUNCTION reject_snapshot_insert()
            """)

        with pytest.raises(Exception, match="injected snapshot persistence failure"):
            await service.verify(version_id)

        assert sends == []
        assert service.version(version_id)["verification_status"] == "FAILED"
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
            connection.execute("DROP TRIGGER reject_snapshot_insert_before_send ON model_input_snapshots")
            connection.execute("DROP FUNCTION reject_snapshot_insert()")
    finally:
        db.close()
