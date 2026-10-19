"""PostgreSQL checks for notification identity, upgrade, and a single claim."""
import asyncio
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.db import POSTGRES_SCHEMA_HEAD, Database
from app.notifications import NotificationService
from db_target_guard import assert_isolated_test_database

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_NOTIFICATION_HEAD = "20261013_0029"


def test_dispatch_key_is_unique_and_channels_can_stay_unowned(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    with db.transaction() as connection:
        columns = {
            row["column_name"]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='notification_deliveries'"
            )
        }
        connection.execute(
            "INSERT INTO notification_channels(id,name,channel_type,secret_env_name,enabled,created_at,updated_at,owner_id) "
            "VALUES ('ch-pg','n','webhook','HOOK',1,'t','t',NULL)"
        )
        connection.execute(
            "INSERT INTO notification_deliveries(id,channel_id,purpose,dispatch_key,attempt,status,created_at) "
            "VALUES ('d1','ch-pg','test','test:ch-pg:1',1,'UNKNOWN','t')"
        )
    assert {"owner_id", "purpose", "dispatch_key", "error_code", "provider_ref", "claimed_at"} <= columns
    with pytest.raises(Exception):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries(id,channel_id,purpose,dispatch_key,attempt,status,created_at) "
                "VALUES ('d2','ch-pg','test','test:ch-pg:1',1,'PENDING','t')"
            )
    with db.connection() as connection:
        owner = connection.execute("SELECT owner_id FROM notification_channels WHERE id='ch-pg'").fetchone()["owner_id"]
    assert owner is None


@pytest.mark.asyncio
async def test_postgres_claim_sends_once(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")

    class Gate(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_async_request(self, request):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return httpx.Response(200, json={"code": 0})

    gate = Gate()
    service = NotificationService(Database(migrated_postgres_url, workspace=tmp_path), transport=gate)
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    key = f"test:{channel.id}:same"

    async def once():
        return await service.send(
            channel.id, "test", "body", None, owner_id="owner-a", purpose="test", dispatch_key=key,
        )

    first_task = asyncio.create_task(once())
    await gate.started.wait()
    second = await once()
    gate.release.set()
    first = await first_task
    assert gate.calls == 1
    assert first.status == "SENT"
    assert second.status in {"SENDING", "SENT"}


def _alembic(database_url: str, revision: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_ROOT / "alembic.ini"), "upgrade", revision],
        cwd=BACKEND_ROOT, env=environment, check=False, capture_output=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, result.stderr[-4000:]


@pytest.fixture
def previous_notification_database(migrated_postgres_url):
    parsed = urlsplit(str(migrated_postgres_url))
    name = parsed.path.lstrip("/")
    url = urlunsplit((parsed.scheme, parsed.netloc, f"/{name}_{uuid.uuid4().hex[:8]}_test", parsed.query, ""))
    assert_isolated_test_database(url)
    database = urlsplit(url).path.lstrip("/")
    admin = urlunsplit((parsed.scheme, parsed.netloc, "/postgres", "", ""))
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))


def test_old_notification_rows_survive_upgrade_from_the_previous_head(previous_notification_database):
    _alembic(previous_notification_database, PREVIOUS_NOTIFICATION_HEAD)
    with psycopg.connect(previous_notification_database, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,created_at,updated_at,deleted_at) VALUES "
            "('thr','live','alice','t','t',NULL),('gone','deleted','bob','t','t','t')"
        )
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES "
            "('turn-live','thr','live','COMPLETED','t','t'),('turn-gone','gone','gone','COMPLETED','t','t')"
        )
        connection.execute(
            "INSERT INTO research_jobs(id,thread_id,source_turn_id,trigger_kind,occurrence_key,topic,"
            "status,phase,available_at,created_at,updated_at) VALUES "
            "('job','thr','turn-live','manual','occ-live','topic','COMPLETED','completed','t','t','t'),"
            "('orphan','gone','turn-gone','manual','occ-gone','topic','COMPLETED','completed','t','t','t')"
        )
        connection.execute(
            "INSERT INTO notification_channels(id,name,channel_type,secret_env_name,enabled,created_at,updated_at) "
            "VALUES ('ch','old','webhook','HOOK',1,'t','t')"
        )
        connection.execute(
            "INSERT INTO notification_deliveries(id,job_id,channel_id,attempt,status,created_at) VALUES "
            "('pending','job','ch',1,'PENDING','t'),('sent','job','ch',2,'SENT','t'),('loose','orphan','ch',1,'PENDING','t')"
        )
    _alembic(previous_notification_database, "head")
    with psycopg.connect(previous_notification_database, row_factory=dict_row) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        channel = connection.execute("SELECT owner_id,deleted_at FROM notification_channels WHERE id='ch'").fetchone()
        rows = {
            row["id"]: row
            for row in connection.execute(
                "SELECT id,status,owner_id,purpose,dispatch_key,prior_delivery_id FROM notification_deliveries"
            )
        }
    assert version == POSTGRES_SCHEMA_HEAD
    assert channel["owner_id"] is None and channel["deleted_at"] is None
    assert rows["pending"]["status"] == "UNKNOWN" and rows["pending"]["owner_id"] == "alice"
    assert rows["sent"]["status"] == "SENT" and rows["sent"]["owner_id"] == "alice"
    assert rows["loose"]["status"] == "UNKNOWN" and rows["loose"]["owner_id"] is None
    assert rows["pending"]["dispatch_key"] == "legacy:pending"
    assert rows["pending"]["prior_delivery_id"] is None
    assert {row["purpose"] for row in rows.values()} == {"report"}


def _seed_report(db, job_id, owner_id):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id,title,owner_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (f"thr-{job_id}", "t", owner_id, "n", "n"),
        )
        connection.execute(
            "INSERT INTO turns(id,thread_id,client_turn_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (f"turn-{job_id}", f"thr-{job_id}", "client", "COMPLETED", "n", "n"),
        )
        connection.execute(
            "INSERT INTO research_jobs(id,thread_id,source_turn_id,trigger_kind,occurrence_key,topic,"
            "source_scopes_json,status,phase,available_at,created_at,updated_at) "
            "VALUES (?,?,?,'manual',?,'topic','[\"web\"]','COMPLETED','completed','n','n','n')",
            (job_id, f"thr-{job_id}", f"turn-{job_id}", f"occ-{job_id}"),
        )
        connection.execute(
            "INSERT INTO research_reports(job_id,title,markdown,partial_markdown,created_at,updated_at,completed_at) "
            "VALUES (?,?,?,'','n','n','n')",
            (job_id, "title", "body"),
        )


def test_postgres_retry_sees_a_sending_row_claimed_under_the_channel_lock(migrated_postgres_url, tmp_path, monkeypatch):
    """An uncommitted SENDING row is invisible until the channel lock is released."""
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path),
        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"code": 0})),
    )
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcome = {}

    def hold_claim():
        with service.db.transaction() as connection:
            connection.execute("SELECT id FROM notification_channels WHERE id=? FOR UPDATE", (channel.id,))
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at) "
                "VALUES ('delivery-hold','owner-a','job',?,'report',?,1,'SENDING','n','n')",
                (channel.id, f"retry:owner-a:job:{channel.id}:held"),
            )
            started.set()
            assert release.wait(timeout=10)

    def challenge():
        assert started.wait(timeout=10)
        try:
            asyncio.run(service.retry("job", channel.id, owner_id="owner-a", idempotency_key="other"))
            outcome["status"] = "sent"
        except ValueError as exc:
            outcome["error"] = str(exc)
        finally:
            finished.set()

    holder = threading.Thread(target=hold_claim)
    challenger = threading.Thread(target=challenge)
    holder.start()
    challenger.start()
    assert started.wait(timeout=10)
    time.sleep(0.4)
    assert not finished.is_set()
    release.set()
    holder.join(timeout=10)
    assert finished.wait(timeout=10)
    challenger.join(timeout=10)
    assert "may duplicate" in outcome.get("error", "")
    assert seen == []
    with service.db.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
        original = connection.execute("SELECT status FROM notification_deliveries WHERE id='delivery-hold'").fetchone()
    assert count == 1 and original["status"] == "SENDING"


def _enable_notify(db, job_id):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO research_schedules(id,name,thread_id,topic,trigger_type,trigger_time,timezone,"
            "notify_enabled,created_at,updated_at) VALUES (?,?,?,?,'daily','09:00','UTC',1,'n','n')",
            (f"sched-{job_id}", "schedule", f"thr-{job_id}", "topic"),
        )
        connection.execute("UPDATE research_jobs SET schedule_id=? WHERE id=?", (f"sched-{job_id}", job_id))


def test_postgres_retry_and_automatic_delivery_interleave_on_the_channel_lock(migrated_postgres_url, tmp_path, monkeypatch):
    """A real retry claim and a real automatic delivery contend for the same channel lock."""
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    database = Database(migrated_postgres_url, workspace=tmp_path)
    transport = httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"code": 0}))
    retry_service = NotificationService(database, transport=transport)
    automatic = NotificationService(database, transport=transport)
    channel = retry_service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(database, "job", "owner-a")
    _enable_notify(database, "job")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcome = {}
    original_lock = NotificationService._lock_channel

    def pausing_lock(self, connection, channel_id):
        original_lock(self, connection, channel_id)
        if not started.is_set():
            started.set()
            assert release.wait(timeout=10)

    monkeypatch.setattr(NotificationService, "_lock_channel", pausing_lock)

    def run_retry():
        asyncio.run(retry_service.retry("job", channel.id, owner_id="owner-a", idempotency_key="first"))

    def run_automatic():
        assert started.wait(timeout=10)
        try:
            outcome["rows"] = asyncio.run(automatic.deliver_completed("job", "title", "body"))
        finally:
            finished.set()

    holder = threading.Thread(target=run_retry)
    challenger = threading.Thread(target=run_automatic)
    holder.start()
    assert started.wait(timeout=10)
    challenger.start()
    time.sleep(0.4)
    assert not finished.is_set()
    release.set()
    holder.join(timeout=10)
    assert finished.wait(timeout=10)
    challenger.join(timeout=10)
    assert len(seen) == 1
    assert len(outcome["rows"]) == 1
    with database.connection() as connection:
        rows = connection.execute("SELECT id,status FROM notification_deliveries").fetchall()
        job = connection.execute("SELECT status FROM research_jobs WHERE id='job'").fetchone()
    assert len(rows) == 1
    assert outcome["rows"][0].id == rows[0]["id"]
    assert job["status"] == "COMPLETED"


def test_restart_closes_sending_as_unknown_and_does_not_send_again(migrated_postgres_url, tmp_path, monkeypatch):
    """A dead process leaves SENDING. Recovery stays on that row and does not call the provider."""
    import json

    from app.harness_context_store import HarnessContextStore
    from app.trace_query import TraceQuery

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path),
        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"code": 0})),
    )
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    HarnessContextStore(service.db).load_or_create_turn_context(
        "turn-job", owner_id="owner-a", thread_id="thr-job",
    )
    with service.db.transaction() as connection:
        connection.execute(
            "INSERT INTO notification_deliveries("
            "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at) "
            "VALUES ('delivery-restart','owner-a','job',?,'report',?,1,'SENDING','n','n')",
            (channel.id, f"report:job:{channel.id}"),
        )
    recovered = service.recover_interrupted_sends()
    assert [(item.id, item.status, item.error_code) for item in recovered] == [
        ("delivery-restart", "UNKNOWN", "RESULT_UNKNOWN"),
    ]
    assert service.recover_interrupted_sends() == []
    _enable_notify(service.db, "job")
    delivered = asyncio.run(service.deliver_completed("job", "title", "secret-body"))
    assert seen == []
    assert [(item.id, item.status) for item in delivered] == [("delivery-restart", "UNKNOWN")]
    with service.db.connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
        events = connection.execute(
            "SELECT type, data_json FROM thread_events WHERE thread_id=? ORDER BY seq",
            ("thr-job",),
        ).fetchall()
    assert count == 1
    assert [row["type"] for row in events] == ["notification.unknown"]
    payload = json.loads(events[0]["data_json"])
    assert payload["delivery_id"] == "delivery-restart"
    assert payload["status"] == "UNKNOWN"
    assert payload["error_code"] == "RESULT_UNKNOWN"
    assert "title" not in payload and "secret" not in payload and "content" not in payload
    view = TraceQuery(service.db).by_thread("thr-job", owner_id="owner-a")
    unknown = next(event for event in view["events"] if event["type"] == "notification.unknown")
    assert unknown["source"] == "notification"
    assert unknown["outcome"]["status"] == "reconciliation_required"
    assert unknown["outcome"]["effect"] == "unknown"
    assert unknown["outcome"]["error"]["retryable"] is False
    assert "notification" in view["sources"]["connected"]
    assert "notification" not in view["sources"]["deferred"]


def test_disabling_before_the_request_fails_without_a_post(migrated_postgres_url, tmp_path, monkeypatch):
    """The channel can be turned off after the claim and before the provider request."""
    import json

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path),
        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"code": 0})),
    )
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    original = NotificationService._still_allowed

    def disable_then_check(self, cid, job_id, owner_id, purpose, action):
        self.update(cid, owner_id=owner_id, enabled=False)
        return original(self, cid, job_id, owner_id, purpose, action)

    monkeypatch.setattr(NotificationService, "_still_allowed", disable_then_check)
    result = asyncio.run(service.send(
        channel.id, "title", "body", "job", owner_id="owner-a",
    ))
    assert result.status == "FAILED" and result.error_code == "NOT_SENT"
    assert seen == []
    with service.db.connection() as connection:
        rows = connection.execute("SELECT id,status FROM notification_deliveries").fetchall()
        kinds = [
            row["type"] for row in connection.execute(
                "SELECT type FROM thread_events WHERE thread_id=? ORDER BY seq", ("thr-job",),
            )
        ]
        payload = json.loads(connection.execute(
            "SELECT data_json FROM thread_events WHERE thread_id=? AND type='notification.failed'",
            ("thr-job",),
        ).fetchone()["data_json"])
    assert len(rows) == 1 and rows[0]["status"] == "FAILED"
    assert kinds == ["notification.claimed", "notification.failed"]
    assert "title" not in payload and "body" not in payload and "SERVER_KEY" not in json.dumps(payload)


def test_a_test_send_does_not_invent_a_thread_event(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0})),
    )
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    result = asyncio.run(service.test(channel.id, owner_id="owner-a"))
    assert result.status == "SENT"
    with service.db.connection() as connection:
        events = connection.execute("SELECT COUNT(*) FROM thread_events").fetchone()[0]
    assert events == 0
