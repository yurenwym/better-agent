"""Direct model verification uses the same pre-send recorder as routed calls."""
import pytest

from app.db import Database
from app.model_admin import ModelAdminService
from app.model_control import ModelControlStore
from app.model_gateway import ModelGateway
from snapshot_entrypoint_helpers import CommittedSnapshotTransport
from test_model_admin_api import _profile_payload
from test_snapshot_gateway import _answer


@pytest.mark.asyncio
async def test_admin_verification_records_unactivated_profile_binding(tmp_path, monkeypatch):
    db = Database(tmp_path / "admin.db")
    service = ModelAdminService(db, owner_id="admin-owner", control_store=ModelControlStore(db))
    version = service.create_profile(_profile_payload(), validate_capacity=False)["versions"][0]["id"]
    observer = CommittedSnapshotTransport(db, "admin-owner", "CS-MD-01", _answer("OK"))
    async def attempt(gateway, request, *args, **kwargs):
        return await observer(gateway.profile, request)
    monkeypatch.setattr(ModelGateway, "_attempt", attempt)
    monkeypatch.setenv("MODEL_ADMIN_TEST_KEY", "offline-test")
    assert (await service.verify(version))["verification_status"] == "VERIFIED"
    assert observer.send_count == 1 and observer.observations[0]["profile_version_id"] == version
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_channels").fetchone()[0] == 0
