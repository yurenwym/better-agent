"""PostgreSQL checks for notification identity, upgrade, and a single claim."""
import asyncio
import contextlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


@pytest.fixture(autouse=True)
def notification_execution_scope(tmp_path, monkeypatch):
    """One local scope for every service in this test, independent of Database.workspace."""
    scope = tmp_path / "notification-scope"
    scope.mkdir()
    monkeypatch.setenv("BETTER_NOTIFICATION_SCOPE_DIR", str(scope))
    return scope


@pytest.fixture(autouse=True)
def close_opened_databases(monkeypatch):
    """Close pools this file opens. A second close is harmless; a close error fails the test."""
    opened = []
    original = Database.__init__

    def tracking(self, *args, **kwargs):
        original(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(Database, "__init__", tracking)
    yield
    for db in opened:
        db.close()


def test_dispatch_key_is_unique_and_channels_can_stay_unowned(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path)
    try:
        _dispatch_key_is_unique(db)
    finally:
        db.close()


def _dispatch_key_is_unique(db):
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
    assert {
        "owner_id", "purpose", "dispatch_key", "error_code", "provider_ref", "claimed_at",
        "recovery_scope_id", "executor_token", "executor_file_dev", "executor_file_ino",
    } <= columns
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
    try:
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
    finally:
        service.db.close()


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
                "SELECT id,status,owner_id,purpose,dispatch_key,prior_delivery_id,recovery_scope_id,executor_token,"
                "executor_file_dev,executor_file_ino "
                "FROM notification_deliveries"
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
    assert all(
        row["recovery_scope_id"] is None and row["executor_token"] is None
        and row["executor_file_dev"] is None and row["executor_file_ino"] is None
        for row in rows.values()
    )


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
    service.db.close()


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
    database.close()


def test_sending_without_execution_identity_is_retained(migrated_postgres_url, tmp_path, monkeypatch):
    """A historical SENDING row with no scope or token is reported and left unchanged."""
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path / "workspace"),
        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"code": 0})),
    )
    try:
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(service.db, "job", "owner-a")
        with service.db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at) "
                "VALUES ('delivery-restart','owner-a','job',?,'report',?,1,'SENDING','n','n')",
                (channel.id, f"report:job:{channel.id}"),
            )
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        assert report.retained == (("delivery-restart", "missing-identity"),)
        assert service.recover_interrupted_sends().recovered == ()
        _enable_notify(service.db, "job")
        delivered = asyncio.run(service.deliver_completed("job", "title", "secret-body"))
        assert seen == []
        assert [(item.id, item.status) for item in delivered] == [("delivery-restart", "SENDING")]
        with service.db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
            status = connection.execute(
                "SELECT status, recovery_scope_id, executor_token FROM notification_deliveries WHERE id='delivery-restart'",
            ).fetchone()
            events = connection.execute(
                "SELECT type FROM thread_events WHERE thread_id=?", ("thr-job",),
            ).fetchall()
        assert count == 1
        assert status["status"] == "SENDING"
        assert status["recovery_scope_id"] is None and status["executor_token"] is None
        assert [row["type"] for row in events] == []
    finally:
        service.db.close()


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


@pytest.mark.asyncio
async def test_blocked_provider_success_stays_sent_after_another_service_recovers(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    """A live send stays SENT when another service runs recovery during HTTP.

    Two pools share one database. A blocks inside the fake provider; B recovers;
    A is then released with a success response. The stored row and one
    notification.sent event are the outcome. notification.unknown is absent.
    """
    from app.harness_context_store import HarnessContextStore

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
    sender_db = Database(migrated_postgres_url, workspace=tmp_path / "sender")
    recoverer_db = Database(migrated_postgres_url, workspace=tmp_path / "recoverer")
    running = None
    try:
        sender = NotificationService(sender_db, transport=gate)
        recoverer = NotificationService(recoverer_db, transport=gate)
        channel = sender.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(sender_db, "job", "owner-a")
        HarnessContextStore(sender_db).load_or_create_turn_context(
            "turn-job", owner_id="owner-a", thread_id="thr-job",
        )
        running = asyncio.create_task(sender.send(
            channel.id, "title", "body", "job", owner_id="owner-a",
        ))
        await asyncio.wait_for(gate.started.wait(), timeout=10)
        recoverer.recover_interrupted_sends()
        gate.release.set()
        result = await asyncio.wait_for(running, timeout=10)
        with sender_db.connection() as connection:
            stored = connection.execute(
                "SELECT status FROM notification_deliveries",
            ).fetchone()
            event_types = [
                row["type"]
                for row in connection.execute(
                    "SELECT type FROM thread_events WHERE thread_id=? ORDER BY seq",
                    ("thr-job",),
                )
            ]
        assert gate.calls == 1
        assert stored["status"] == "SENT"
        assert result.status == "SENT"
        assert event_types.count("notification.sent") == 1
        assert "notification.unknown" not in event_types
    finally:
        gate.release.set()
        if running is not None and not running.done():
            running.cancel()
        if running is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(running, timeout=10)
        sender_db.close()
        recoverer_db.close()


class _Gate(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return httpx.Response(200, json={"code": 0})


def _application_url(database_url: str, name: str) -> str:
    parts = urlsplit(str(database_url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["application_name"] = name
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _terminate_backends(database_url: str, name: str) -> None:
    with psycopg.connect(_application_url(database_url, "notify-admin"), autocommit=True) as connection:
        pids = [
            row[0] for row in connection.execute(
                "SELECT pid FROM pg_stat_activity WHERE application_name=%s AND pid<>pg_backend_pid()",
                (name,),
            )
        ]
        assert pids
        for pid in pids:
            connection.execute("SELECT pg_terminate_backend(%s)", (pid,))


def _event_types(db, thread_id="thr-job") -> list[str]:
    with db.connection() as connection:
        return [
            row["type"] for row in connection.execute(
                "SELECT type FROM thread_events WHERE thread_id=? ORDER BY seq", (thread_id,),
            )
        ]


def _status(db, delivery_id: str) -> str:
    with db.connection() as connection:
        return connection.execute(
            "SELECT status FROM notification_deliveries WHERE id=?", (delivery_id,),
        ).fetchone()["status"]


def _leave_unlocked_token(service, token: str) -> tuple[str, str]:
    """A dead executor leaves the regular token file and no kernel lock."""
    fd = os.open(service.execution.lock_dir / token, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    try:
        os.write(fd, b"\0")
        info = os.fstat(fd)
    finally:
        os.close(fd)
    file_dev, file_ino = str(info.st_dev), str(info.st_ino)
    with service.db.transaction() as connection:
        connection.execute(
            "UPDATE notification_deliveries SET executor_file_dev=?, executor_file_ino=? WHERE executor_token=?",
            (file_dev, file_ino, token),
        )
    return file_dev, file_ino


async def _stop_task(gate: _Gate, running: asyncio.Task | None) -> None:
    gate.release.set()
    if running is None:
        return
    if not running.done():
        running.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(running, timeout=10)


def test_empty_database_upgrade_adds_nullable_execution_identity(previous_notification_database):
    _alembic(previous_notification_database, "head")
    with psycopg.connect(previous_notification_database, row_factory=dict_row) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        columns = {
            row["column_name"]: row["is_nullable"]
            for row in connection.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name='notification_deliveries' "
                "AND column_name IN ('recovery_scope_id','executor_token','executor_file_dev','executor_file_ino')"
            )
        }
        count = connection.execute("SELECT COUNT(*) AS count FROM notification_deliveries").fetchone()["count"]
    assert version == POSTGRES_SCHEMA_HEAD
    assert columns == {
        "recovery_scope_id": "YES", "executor_token": "YES",
        "executor_file_dev": "YES", "executor_file_ino": "YES",
    }
    assert count == 0


def test_explicit_recovery_of_an_unlocked_same_scope_send_is_unknown(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    from app.harness_context_store import HarnessContextStore
    from app.trace_query import TraceQuery

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path / "workspace"),
        transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json={"code": 0})),
    )
    try:
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(service.db, "job", "owner-a")
        HarnessContextStore(service.db).load_or_create_turn_context(
            "turn-job", owner_id="owner-a", thread_id="thr-job",
        )
        token = uuid.uuid4().hex
        with service.db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
                "recovery_scope_id,executor_token) "
                "VALUES ('delivery-known','owner-a','job',?,'report',?,1,'SENDING','n','n',?,?)",
                (channel.id, f"report:job:{channel.id}", service.execution.scope_id, token),
            )
        _leave_unlocked_token(service, token)
        first = service.recover_interrupted_sends()
        second = service.recover_interrupted_sends()
        assert [(item.id, item.status, item.error_code) for item in first.recovered] == [
            ("delivery-known", "UNKNOWN", "RESULT_UNKNOWN"),
        ]
        assert second.recovered == ()
        _enable_notify(service.db, "job")
        delivered = asyncio.run(service.deliver_completed("job", "title", "secret-body"))
        assert seen == []
        assert [(item.id, item.status) for item in delivered] == [("delivery-known", "UNKNOWN")]
        with service.db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
            payload = json.loads(connection.execute(
                "SELECT data_json FROM thread_events WHERE thread_id=? AND type='notification.unknown'",
                ("thr-job",),
            ).fetchone()["data_json"])
        assert count == 1
        assert _event_types(service.db) == ["notification.unknown"]
        assert payload["delivery_id"] == "delivery-known"
        assert payload["status"] == "UNKNOWN" and payload["error_code"] == "RESULT_UNKNOWN"
        assert "title" not in payload and "secret" not in payload and "content" not in payload
        view = TraceQuery(service.db).by_thread("thr-job", owner_id="owner-a")
        unknown = next(event for event in view["events"] if event["type"] == "notification.unknown")
        assert unknown["source"] == "notification"
        assert unknown["outcome"]["status"] == "reconciliation_required"
        assert unknown["outcome"]["effect"] == "unknown"
        assert unknown["outcome"]["error"]["retryable"] is False
        assert "notification" in view["sources"]["connected"]
        assert "notification" not in view["sources"]["deferred"]
    finally:
        service.db.close()


def test_null_and_other_scope_sends_are_retained(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path / "workspace"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0})),
    )
    try:
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(service.db, "job", "owner-a")
        with service.db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
                "recovery_scope_id,executor_token) VALUES "
                "('missing','owner-a','job',?,'report','missing',1,'SENDING','n','n',NULL,NULL),"
                "('other','owner-a','job',?,'report','other',1,'SENDING','n','n',?,?)",
                (channel.id, channel.id, "other-host-scope", uuid.uuid4().hex),
            )
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        assert report.retained == (("missing", "missing-identity"), ("other", "other-scope"))
        with service.db.connection() as connection:
            rows = {
                row["id"]: row["status"] for row in connection.execute(
                    "SELECT id, status FROM notification_deliveries ORDER BY id",
                )
            }
        assert rows == {"missing": "SENDING", "other": "SENDING"}
        assert _event_types(service.db) == []
    finally:
        service.db.close()


def test_reopened_token_cannot_store_a_result(migrated_postgres_url, tmp_path, monkeypatch):
    from app.notification_execution import NotificationExecutionUnavailable

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path / "workspace"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0})),
    )
    guard = None
    try:
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(service.db, "job", "owner-a")
        token = uuid.uuid4().hex
        with service.db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
                "recovery_scope_id,executor_token) "
                "VALUES ('delivery-old','owner-a','job',?,'report',?,1,'SENDING','n','n',?,?)",
                (channel.id, f"manual:{token}", service.execution.scope_id, token),
            )
        file_dev, file_ino = _leave_unlocked_token(service, token)
        guard = service.execution.try_acquire(token, file_dev=file_dev, file_ino=file_ino)
        assert guard is not None
        with pytest.raises(NotificationExecutionUnavailable):
            service._finish("delivery-old", "SENT", guard=guard)
        assert _status(service.db, "delivery-old") == "SENDING"
        assert "notification.sent" not in _event_types(service.db)
    finally:
        if guard is not None:
            guard.release()
        service.db.close()


def test_unknown_event_failure_rolls_back_the_status(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    service = NotificationService(
        Database(migrated_postgres_url, workspace=tmp_path / "workspace"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0})),
    )
    original = NotificationService._trace

    def fail_unknown(self, connection, delivery, event_type):
        if event_type == "notification.unknown":
            raise RuntimeError("event failed")
        return original(self, connection, delivery, event_type)

    monkeypatch.setattr(NotificationService, "_trace", fail_unknown)
    try:
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(service.db, "job", "owner-a")
        token = uuid.uuid4().hex
        with service.db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
                "recovery_scope_id,executor_token) "
                "VALUES ('delivery-rollback','owner-a','job',?,'report',?,1,'SENDING','n','n',?,?)",
                (channel.id, "rollback-key", service.execution.scope_id, token),
            )
        _leave_unlocked_token(service, token)
        with pytest.raises(RuntimeError, match="event failed"):
            service.recover_interrupted_sends()
        assert _status(service.db, "delivery-rollback") == "SENDING"
        assert _event_types(service.db) == []
        monkeypatch.setattr(NotificationService, "_trace", original)
        recovered = service.recover_interrupted_sends()
        assert [item.id for item in recovered.recovered] == ["delivery-rollback"]
        assert _event_types(service.db) == ["notification.unknown"]
    finally:
        service.db.close()


def test_two_recoverers_change_one_unlocked_row_once(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    first_db = Database(migrated_postgres_url, workspace=tmp_path / "first")
    second_db = Database(migrated_postgres_url, workspace=tmp_path / "second")
    try:
        first = NotificationService(first_db, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        second = NotificationService(second_db, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        channel = first.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(first_db, "job", "owner-a")
        token = uuid.uuid4().hex
        with first_db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
                "recovery_scope_id,executor_token) "
                "VALUES ('delivery-race','owner-a','job',?,'report',?,1,'SENDING','n','n',?,?)",
                (channel.id, "race-key", first.execution.scope_id, token),
            )
        _leave_unlocked_token(first, token)
        barrier = threading.Barrier(2)
        reports = []

        def recover(service):
            barrier.wait(timeout=10)
            reports.append(service.recover_interrupted_sends())

        workers = [threading.Thread(target=recover, args=(service,)) for service in (first, second)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        assert len(reports) == 2
        assert [item.id for report in reports for item in report.recovered] == ["delivery-race"]
        assert _status(first_db, "delivery-race") == "UNKNOWN"
        assert _event_types(first_db).count("notification.unknown") == 1
        assert first.recover_interrupted_sends().recovered == ()
    finally:
        first_db.close()
        second_db.close()


@pytest.mark.asyncio
async def test_cancelled_send_can_be_recovered_without_another_provider_call(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    gate = _Gate()
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    running = None
    try:
        service = NotificationService(db, transport=gate)
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        running = asyncio.create_task(service.send(
            channel.id, "title", "body", "job", owner_id="owner-a",
        ))
        await asyncio.wait_for(gate.started.wait(), timeout=10)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=10)
        running = None
        report = service.recover_interrupted_sends()
        assert [item.status for item in report.recovered] == ["UNKNOWN"]
        original_id = report.recovered[0].id
        assert gate.calls == 1
        assert _event_types(db).count("notification.unknown") == 1
        replay = await asyncio.wait_for(service.send(
            channel.id, "title", "body", "job", owner_id="owner-a",
            dispatch_key=f"report:job:{channel.id}",
        ), timeout=10)
        assert replay.id == original_id and replay.status == "UNKNOWN"
        assert gate.calls == 1
        gate.release.set()
        confirmed = await asyncio.wait_for(service.retry(
            "job", channel.id, owner_id="owner-a", idempotency_key="next",
            confirm_duplicate=True, prior_delivery_id=original_id,
        ), timeout=10)
        repeated = await asyncio.wait_for(service.retry(
            "job", channel.id, owner_id="owner-a", idempotency_key="next",
            confirm_duplicate=True, prior_delivery_id=original_id,
        ), timeout=10)
        assert confirmed.status == "SENT" and confirmed.id != original_id
        assert repeated.id == confirmed.id
        assert gate.calls == 2
        assert _status(db, original_id) == "UNKNOWN"
        with db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
        assert count == 2
    finally:
        await _stop_task(gate, running)
        db.close()


@pytest.mark.asyncio
async def test_database_disconnect_does_not_unlock_an_active_send(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    gate = _Gate()
    sender_db = Database(
        _application_url(migrated_postgres_url, "notify-sender"), workspace=tmp_path / "sender",
    )
    recoverer_db = Database(
        _application_url(migrated_postgres_url, "notify-recover"), workspace=tmp_path / "recoverer",
    )
    running = None
    try:
        sender = NotificationService(sender_db, transport=gate)
        recoverer = NotificationService(recoverer_db, transport=gate)
        channel = sender.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(sender_db, "job", "owner-a")
        running = asyncio.create_task(sender.send(
            channel.id, "title", "body", "job", owner_id="owner-a",
        ))
        await asyncio.wait_for(gate.started.wait(), timeout=10)
        _terminate_backends(migrated_postgres_url, "notify-sender")
        assert recoverer.recover_interrupted_sends().recovered == ()
        with recoverer_db.connection() as connection:
            status = connection.execute("SELECT status FROM notification_deliveries").fetchone()["status"]
        assert status == "SENDING"
        assert gate.calls == 1
        assert "notification.unknown" not in _event_types(recoverer_db)
        assert "notification.sent" not in _event_types(recoverer_db)
    finally:
        await _stop_task(gate, running)
        sender_db.close()
        recoverer_db.close()


def test_terminated_sender_process_is_recovered_once(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    db = Database(migrated_postgres_url, workspace=tmp_path / "parent")
    gate = tmp_path / "gate"
    gate.mkdir()
    proc = None
    stdout = stderr = None
    try:
        service = NotificationService(db, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(BACKEND_ROOT)
        env["DATABASE_URL"] = str(migrated_postgres_url)
        env["NOTIFY_BLOCKED_SEND"] = "1"
        env["NOTIFY_GATE_DIR"] = str(gate)
        env["NOTIFY_WORKSPACE"] = str(tmp_path / "child-workspace")
        env["NOTIFY_CHANNEL_ID"] = channel.id
        env.pop("TEST_DATABASE_URL", None)
        stdout = (tmp_path / "child-out.txt").open("w", encoding="utf-8")
        stderr = (tmp_path / "child-err.txt").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__))],
            cwd=BACKEND_ROOT, env=env, stdout=stdout, stderr=stderr,
        )
        deadline = time.monotonic() + 20
        while not (gate / "started").exists():
            if time.monotonic() > deadline or proc.poll() is not None:
                stderr.flush()
                raise AssertionError((tmp_path / "child-err.txt").read_text(encoding="utf-8", errors="replace")[-2000:])
            time.sleep(0.05)
        assert service.recover_interrupted_sends().recovered == ()
        with db.connection() as connection:
            before = connection.execute("SELECT id, status FROM notification_deliveries").fetchall()
        assert len(before) == 1 and before[0]["status"] == "SENDING"
        proc.terminate()
        assert proc.wait(timeout=10) is not None
        proc = None
        recovered = service.recover_interrupted_sends()
        assert [(item.id, item.status) for item in recovered.recovered] == [(before[0]["id"], "UNKNOWN")]
        assert service.recover_interrupted_sends().recovered == ()
        with db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
        assert count == 1
        assert (gate / "started").read_text(encoding="utf-8") == "1"
        assert _event_types(db).count("notification.unknown") == 1
        assert "notification.sent" not in _event_types(db)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        if stdout is not None:
            stdout.close()
        if stderr is not None:
            stderr.close()
        db.close()


def test_explicit_recovery_command_reports_retained_rows(migrated_postgres_url, tmp_path, monkeypatch, notification_execution_scope):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    source = (BACKEND_ROOT / "app" / "notification_recovery.py").read_text(encoding="utf-8")
    assert "build_runtime" not in source and "ManagedResearchWorker" not in source
    db = Database(migrated_postgres_url, workspace=tmp_path / "seed")
    try:
        service = NotificationService(db, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        token = uuid.uuid4().hex
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
                "recovery_scope_id,executor_token) VALUES "
                "('kept','owner-a','job',?,'report','kept',1,'SENDING','n','n',NULL,NULL),"
                "('ready','owner-a','job',?,'report','ready',1,'SENDING','n','n',?,?)",
                (channel.id, channel.id, service.execution.scope_id, token),
            )
        _leave_unlocked_token(service, token)
    finally:
        db.close()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["DATABASE_URL"] = str(migrated_postgres_url)
    env["BETTER_NOTIFICATION_SCOPE_DIR"] = str(notification_execution_scope)
    env.pop("TEST_DATABASE_URL", None)
    completed = subprocess.run(
        [sys.executable, "-m", "app.notification_recovery", "--workspace", str(tmp_path / "command")],
        cwd=BACKEND_ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    payload = json.loads(completed.stdout)
    assert payload["recovered"] == [{"id": "ready", "status": "UNKNOWN"}]
    assert payload["retained"] == [{"id": "kept", "reason": "missing-identity"}]
    check = Database(migrated_postgres_url, workspace=tmp_path / "check")
    try:
        with check.connection() as connection:
            rows = {
                row["id"]: row["status"] for row in connection.execute("SELECT id, status FROM notification_deliveries")
            }
        assert rows == {"kept": "SENDING", "ready": "UNKNOWN"}
    finally:
        check.close()


def test_startup_and_research_worker_do_not_recover_notifications():
    from app import startup
    from app.research.worker import ManagedResearchWorker

    assert "recover_interrupted_sends" not in inspect.getsource(startup.build_runtime)
    assert "recover_interrupted_sends" not in inspect.getsource(ManagedResearchWorker.start)


@pytest.mark.asyncio
async def test_research_worker_start_does_not_call_notification_recovery():
    from app.research.worker import ManagedResearchWorker

    class Notifier:
        def recover_interrupted_sends(self):
            raise AssertionError("worker start recovered notifications")

    class Service:
        notifications = Notifier()

        def claim_next(self, owner, lease_seconds):
            return None

    worker = ManagedResearchWorker(Service(), poll_interval=0.05)
    await worker.start()
    await worker.stop()


class _CloseGate(httpx.AsyncBaseTransport):
    def __init__(self, close_mode="wait"):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.close_mode = close_mode

    async def handle_async_request(self, request):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return httpx.Response(200, json={"code": 0})

    async def aclose(self):
        self.close_started.set()
        if self.close_mode == "raise":
            raise RuntimeError("close failed")
        if self.close_mode == "cancel":
            raise asyncio.CancelledError()
        await self.close_release.wait()


def _sending_id(db) -> str:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT id FROM notification_deliveries WHERE status='SENDING'",
        ).fetchone()
    assert row is not None
    return row["id"]


async def _finish_close(gate: _CloseGate, running: asyncio.Task | None) -> None:
    gate.release.set()
    gate.close_release.set()
    if running is None:
        return
    if not running.done():
        running.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(running, timeout=10)


@pytest.mark.asyncio
async def test_cancel_during_close_keeps_the_guard_until_close_succeeds(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    gate = _CloseGate()
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    running = None
    try:
        service = NotificationService(db, transport=gate)
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        running = asyncio.create_task(service.send(
            channel.id, "title", "body", "job", owner_id="owner-a",
        ))
        await asyncio.wait_for(gate.started.wait(), timeout=10)
        running.cancel()
        await asyncio.wait_for(gate.close_started.wait(), timeout=10)
        running.cancel()
        assert service.recover_interrupted_sends().recovered == ()
        delivery_id = _sending_id(db)
        gate.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=10)
        running = None
        recovered = service.recover_interrupted_sends()
        assert [item.id for item in recovered.recovered] == [delivery_id]
        assert gate.calls == 1
        assert _status(db, delivery_id) == "UNKNOWN"
    finally:
        await _finish_close(gate, running)
        db.close()


@pytest.mark.asyncio
async def test_close_error_and_self_cancel_do_not_release_the_guard(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    from app.notification_execution import NotificationExecutionUnavailable

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    running = None
    gate = _CloseGate("raise")
    try:
        service = NotificationService(db, transport=gate)
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        running = asyncio.create_task(service.send(
            channel.id, "title", "body", "job", owner_id="owner-a",
        ))
        await asyncio.wait_for(gate.started.wait(), timeout=10)
        running.cancel()
        with pytest.raises(RuntimeError, match="close failed"):
            await asyncio.wait_for(running, timeout=10)
        running = None
        delivery_id = _sending_id(db)
        assert service.recover_interrupted_sends().recovered == ()
        assert _status(db, delivery_id) == "SENDING"
        assert gate.calls == 1
        gate.close_mode = "cancel"
        gate.started.clear()
        running = asyncio.create_task(service.send(
            channel.id, "title", "again", None, owner_id="owner-a", purpose="test", action="send",
            dispatch_key=f"test:{channel.id}:{uuid.uuid4().hex}",
        ))
        await asyncio.wait_for(gate.started.wait(), timeout=10)
        running.cancel()
        with pytest.raises(NotificationExecutionUnavailable):
            await asyncio.wait_for(running, timeout=10)
        running = None
        assert service.recover_interrupted_sends().recovered == ()
        assert _status(db, delivery_id) == "SENDING"
    finally:
        await _finish_close(gate, running)
        db.close()


@pytest.mark.asyncio
async def test_both_result_writes_failing_can_be_recovered_after_close(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    attempts = []

    def fail_finish(self, delivery_id, status, *, guard, **fields):
        attempts.append(status)
        raise RuntimeError("store failed")

    monkeypatch.setattr(NotificationService, "_finish", fail_finish)
    seen = []
    sender_db = Database(migrated_postgres_url, workspace=tmp_path / "sender")
    recoverer_db = Database(migrated_postgres_url, workspace=tmp_path / "recoverer")
    try:
        sender = NotificationService(
            sender_db,
            transport=httpx.MockTransport(
                lambda request: seen.append(request) or httpx.Response(200, json={"code": 0}),
            ),
        )
        recoverer = NotificationService(
            recoverer_db, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0})),
        )
        channel = sender.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(sender_db, "job", "owner-a")
        with pytest.raises(RuntimeError, match="store failed"):
            await sender.send(channel.id, "title", "body", "job", owner_id="owner-a")
        assert attempts == ["SENT", "UNKNOWN"]
        assert len(seen) == 1
        with sender_db.connection() as connection:
            rows = connection.execute("SELECT id, status FROM notification_deliveries").fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "SENDING"
        recovered = recoverer.recover_interrupted_sends()
        assert [(item.id, item.status) for item in recovered.recovered] == [(rows[0]["id"], "UNKNOWN")]
        assert recoverer.recover_interrupted_sends().recovered == ()
        assert _event_types(recoverer_db).count("notification.unknown") == 1
        with recoverer_db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
        assert count == 1
    finally:
        sender_db.close()
        recoverer_db.close()


def _replace_regular_file(path: Path) -> str:
    moved = path.with_name(path.name + ".moved")
    try:
        os.replace(path, moved)
    except OSError:
        return "refused"
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, b"\0")
    finally:
        os.close(fd)
    return "replaced"


def test_token_file_replaced_before_lock_stays_sending(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        service = NotificationService(db, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        token = uuid.uuid4().hex
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
                "recovery_scope_id,executor_token) "
                "VALUES ('delivery-replaced','owner-a','job',?,'report','replaced',1,'SENDING','n','n',?,?)",
                (channel.id, service.execution.scope_id, token),
            )
        _leave_unlocked_token(service, token)
        assert _replace_regular_file(service.execution.lock_dir / token) == "replaced"
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        assert report.retained == (("delivery-replaced", "untrusted-token-file"),)
        assert _status(db, "delivery-replaced") == "SENDING"
    finally:
        db.close()


def test_replaced_lock_directory_stays_sending(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        service = NotificationService(db, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        token = uuid.uuid4().hex
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries("
                "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
                "recovery_scope_id,executor_token) "
                "VALUES ('delivery-dir','owner-a','job',?,'report','dir',1,'SENDING','n','n',?,?)",
                (channel.id, service.execution.scope_id, token),
            )
        _leave_unlocked_token(service, token)
        moved = service.execution.directory / "locks-moved"
        os.replace(service.execution.lock_dir, moved)
        service.execution.lock_dir.mkdir()
        fd = os.open(service.execution.lock_dir / token, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, b"\0")
        finally:
            os.close(fd)
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        assert report.retained == (("delivery-dir", "untrusted-scope"),)
        assert _status(db, "delivery-dir") == "SENDING"
        assert moved.is_dir()
    finally:
        db.close()


def _spawn_blocked_send(database_url, workspace, channel_id, gate: Path, *, timeout: float = 20, command=None):
    """Start a blocked sender. Failures close the process and both handles before raising."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["DATABASE_URL"] = str(database_url)
    env["NOTIFY_BLOCKED_SEND"] = "1"
    env["NOTIFY_GATE_DIR"] = str(gate)
    env["NOTIFY_WORKSPACE"] = str(workspace)
    env["NOTIFY_CHANNEL_ID"] = channel_id
    env.pop("TEST_DATABASE_URL", None)
    stdout = stderr = proc = None
    out_path = gate.parent / "replace-out.txt"
    err_path = gate.parent / "replace-err.txt"

    def _close_handles() -> None:
        nonlocal stdout, stderr
        for handle in (stdout, stderr):
            if handle is not None and not handle.closed:
                handle.close()
        stdout = stderr = None

    def _stop_proc() -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    try:
        stdout = out_path.open("w", encoding="utf-8")
        stderr = err_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            command or [sys.executable, str(Path(__file__))],
            cwd=BACKEND_ROOT, env=env, stdout=stdout, stderr=stderr,
        )
    except Exception:
        _stop_proc()
        _close_handles()
        raise
    try:
        deadline = time.monotonic() + timeout
        while not (gate / "started").exists():
            if time.monotonic() > deadline or proc.poll() is not None:
                stderr.flush()
                detail = err_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise AssertionError(detail or "blocked sender did not reach the gate")
            time.sleep(0.05)
    except Exception:
        _stop_proc()
        _close_handles()
        raise
    return proc, stdout, stderr


def _stop_process(proc, stdout, stderr) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=10)
    if stdout is not None:
        stdout.close()
    if stderr is not None:
        stderr.close()


def test_another_process_keeps_sending_when_the_token_file_is_replaced(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    db = Database(migrated_postgres_url, workspace=tmp_path / "parent")
    gate = tmp_path / "gate-file"
    gate.mkdir()
    proc = stdout = stderr = None
    try:
        service = NotificationService(db, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        proc, stdout, stderr = _spawn_blocked_send(
            migrated_postgres_url, tmp_path / "child", channel.id, gate,
        )
        with db.connection() as connection:
            row = connection.execute(
                "SELECT id, executor_token, status FROM notification_deliveries",
            ).fetchone()
        assert row["status"] == "SENDING"
        outcome = _replace_regular_file(service.execution.lock_dir / row["executor_token"])
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        if outcome == "replaced":
            assert report.retained == ((row["id"], "untrusted-token-file"),)
        assert _status(db, row["id"]) == "SENDING"
        assert (gate / "started").read_text(encoding="utf-8") == "1"
    finally:
        _stop_process(proc, stdout, stderr)
        db.close()


def test_another_process_keeps_sending_when_the_lock_directory_is_replaced(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    db = Database(migrated_postgres_url, workspace=tmp_path / "parent")
    gate = tmp_path / "gate-dir"
    gate.mkdir()
    proc = stdout = stderr = None
    try:
        service = NotificationService(db, transport=httpx.MockTransport(lambda request: httpx.Response(200)))
        channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
        _seed_report(db, "job", "owner-a")
        proc, stdout, stderr = _spawn_blocked_send(
            migrated_postgres_url, tmp_path / "child-dir", channel.id, gate,
        )
        with db.connection() as connection:
            row = connection.execute("SELECT id, executor_token, status FROM notification_deliveries").fetchone()
        assert row["status"] == "SENDING"
        moved = service.execution.directory / "locks-held-moved"
        try:
            os.replace(service.execution.lock_dir, moved)
        except OSError:
            replaced = False
        else:
            replaced = True
            service.execution.lock_dir.mkdir()
            fd = os.open(service.execution.lock_dir / row["executor_token"], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, b"\0")
            finally:
                os.close(fd)
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        if replaced:
            assert (row["id"], "untrusted-scope") in report.retained
        assert _status(db, row["id"]) == "SENDING"
        assert (gate / "started").read_text(encoding="utf-8") == "1"
    finally:
        _stop_process(proc, stdout, stderr)
        db.close()


def _seed_sending(service, delivery_id: str, token: str) -> None:
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    with service.db.transaction() as connection:
        connection.execute(
            "INSERT INTO notification_deliveries("
            "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,claimed_at,"
            "recovery_scope_id,executor_token) "
            "VALUES (?, 'owner-a','job',?,'report',?,1,'SENDING','n','n',?,?)",
            (delivery_id, channel.id, delivery_id, service.execution.scope_id, token),
        )


def _inject_before_lock(monkeypatch, mutate):
    import app.notification_execution as execution

    observed = {}
    real_lock = execution._lock_fd

    def inject(fd, *, blocking):
        observed["called"] = observed.get("called", 0) + 1
        mutate(observed)
        return real_lock(fd, blocking=blocking)

    monkeypatch.setattr(execution, "_lock_fd", inject)
    return observed


def test_replacement_between_check_and_lock_is_refused(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        service = NotificationService(
            db, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
        )
        token = uuid.uuid4().hex
        _seed_sending(service, "delivery-window", token)
        _leave_unlocked_token(service, token)
        path = service.execution.lock_dir / token
        identity = service.execution.directory / "scope-identity.json"
        before_file = path.lstat()
        before_identity = identity.read_bytes()

        def mutate(observed):
            try:
                os.replace(path, path.with_name(token + ".moved"))
            except OSError as exc:
                observed["outcome"] = "refused"
                observed["error"] = exc
            else:
                observed["outcome"] = "replaced"
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, b"\0")
                finally:
                    os.close(fd)

        observed = _inject_before_lock(monkeypatch, mutate)
        report = service.recover_interrupted_sends()
        assert observed["called"] == 1
        assert seen == []
        if observed["outcome"] == "replaced":
            assert report.recovered == ()
            assert report.retained == (("delivery-window", "untrusted-token-file"),)
            assert _status(db, "delivery-window") == "SENDING"
            assert "notification.unknown" not in _event_types(db)
        else:
            assert observed["outcome"] == "refused" and isinstance(observed["error"], OSError)
            after = path.lstat()
            assert (after.st_dev, after.st_ino) == (before_file.st_dev, before_file.st_ino)
            assert identity.read_bytes() == before_identity
            assert [(item.id, item.status) for item in report.recovered] == [("delivery-window", "UNKNOWN")]
            assert report.retained == ()
            assert _event_types(db).count("notification.unknown") == 1
    finally:
        db.close()


def test_directory_replacement_between_check_and_lock_is_refused(
    migrated_postgres_url, tmp_path, monkeypatch,
):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        service = NotificationService(
            db, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
        )
        token = uuid.uuid4().hex
        _seed_sending(service, "delivery-dir-window", token)
        _leave_unlocked_token(service, token)
        lock_dir = service.execution.lock_dir
        identity = service.execution.directory / "scope-identity.json"
        before_dir = lock_dir.lstat()
        before_file = (lock_dir / token).lstat()
        before_identity = identity.read_bytes()
        moved = service.execution.directory / "locks-window-moved"

        def mutate(observed):
            try:
                os.replace(lock_dir, moved)
            except OSError as exc:
                observed["outcome"] = "refused"
                observed["error"] = exc
            else:
                observed["outcome"] = "replaced"
                lock_dir.mkdir()
                os.link(moved / token, lock_dir / token)

        observed = _inject_before_lock(monkeypatch, mutate)
        report = service.recover_interrupted_sends()
        assert observed["called"] == 1
        assert seen == []
        if observed["outcome"] == "replaced":
            assert (lock_dir / token).is_file() and not (lock_dir / token).is_symlink()
            assert report.recovered == ()
            assert report.retained == (("delivery-dir-window", "untrusted-scope"),)
            assert _status(db, "delivery-dir-window") == "SENDING"
            assert "notification.unknown" not in _event_types(db)
        else:
            assert observed["outcome"] == "refused" and isinstance(observed["error"], OSError)
            assert (lock_dir.lstat().st_dev, lock_dir.lstat().st_ino) == (before_dir.st_dev, before_dir.st_ino)
            after_file = (lock_dir / token).lstat()
            assert (after_file.st_dev, after_file.st_ino) == (before_file.st_dev, before_file.st_ino)
            assert identity.read_bytes() == before_identity
            assert [(item.id, item.status) for item in report.recovered] == [("delivery-dir-window", "UNKNOWN")]
            assert report.retained == ()
            assert _event_types(db).count("notification.unknown") == 1
    finally:
        db.close()


def test_identity_fault_after_lock_is_refused(migrated_postgres_url, tmp_path, monkeypatch):
    import app.notification_execution as execution

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        service = NotificationService(
            db, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
        )
        token = uuid.uuid4().hex
        _seed_sending(service, "delivery-after-lock", token)
        _leave_unlocked_token(service, token)
        observed = {"called": 0}
        real_lock = execution._lock_fd

        def inject(fd, *, blocking):
            observed["called"] += 1
            real_lock(fd, blocking=blocking)
            identity = service.execution.directory / "scope-identity.json"
            payload = json.loads(identity.read_text(encoding="utf-8"))
            payload["locks_ino"] = "not-the-original"
            identity.write_text(json.dumps(payload), encoding="utf-8")

        monkeypatch.setattr(execution, "_lock_fd", inject)
        report = service.recover_interrupted_sends()
        assert observed["called"] == 1
        assert report.recovered == ()
        assert report.retained == (("delivery-after-lock", "untrusted-scope"),)
        assert _status(db, "delivery-after-lock") == "SENDING"
        assert seen == []
        assert "notification.unknown" not in _event_types(db)
    finally:
        db.close()


def test_copied_scope_identity_is_refused(migrated_postgres_url, tmp_path):
    from app.notification_execution import NotificationExecutionUnavailable

    scope_x = tmp_path / "scope-x"
    scope_y = tmp_path / "scope-y"
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        original_service = NotificationService(
            db, transport=httpx.MockTransport(lambda request: httpx.Response(200)), execution_scope=scope_x,
        )
        token = uuid.uuid4().hex
        fd = os.open(scope_x / "locks" / token, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, b"\0")
        finally:
            os.close(fd)
        original = (scope_x / "scope-identity.json").read_bytes()
        shutil.copytree(scope_x, scope_y)
        assert (scope_y / "locks" / token).is_file()
        assert (scope_y / "scope-identity.json").read_bytes() == original
        with pytest.raises(NotificationExecutionUnavailable):
            NotificationService(
                db, transport=httpx.MockTransport(lambda request: httpx.Response(200)), execution_scope=scope_y,
            )
        assert (scope_y / "scope-identity.json").read_bytes() == original
        assert (scope_y / "locks").is_dir()
        trusted = NotificationService(
            db, transport=httpx.MockTransport(lambda request: httpx.Response(200)), execution_scope=scope_x,
        )
        assert trusted.execution.scope_id == original_service.execution.scope_id
    finally:
        db.close()


def test_missing_token_file_is_not_created(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        service = NotificationService(
            db, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
        )
        token = uuid.uuid4().hex
        _seed_sending(service, "delivery-missing-file", token)
        with db.transaction() as connection:
            connection.execute(
                "UPDATE notification_deliveries SET executor_file_dev='9', executor_file_ino='9' WHERE id='delivery-missing-file'",
            )
        path = service.execution.lock_dir / token
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        assert report.retained == (("delivery-missing-file", "missing-token-file"),)
        assert not path.exists()
        assert _status(db, "delivery-missing-file") == "SENDING"
        assert seen == []
        assert "notification.unknown" not in _event_types(db)
    finally:
        db.close()


def test_token_symlink_or_lstat_fault_is_refused(migrated_postgres_url, tmp_path, monkeypatch):
    import stat

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        service = NotificationService(
            db, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
        )
        token = uuid.uuid4().hex
        _seed_sending(service, "delivery-link", token)
        _leave_unlocked_token(service, token)
        path = service.execution.lock_dir / token
        target = tmp_path / "outside-token"
        target.write_bytes(b"keep")
        try:
            os.remove(path)
            os.symlink(target, path)
        except OSError:
            if not path.exists():
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, b"\0")
                finally:
                    os.close(fd)
            real_lstat = Path.lstat

            def fault_lstat(self):
                info = real_lstat(self)
                if self.name != token:
                    return info

                class _Symlink:
                    st_mode = stat.S_IFLNK | 0o777
                    st_dev = info.st_dev
                    st_ino = info.st_ino

                return _Symlink()

            monkeypatch.setattr(Path, "lstat", fault_lstat)
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        assert report.retained == (("delivery-link", "untrusted-token-file"),)
        assert _status(db, "delivery-link") == "SENDING"
        assert target.read_bytes() == b"keep"
        assert seen == []
        assert "notification.unknown" not in _event_types(db)
    finally:
        db.close()


def test_missing_file_identity_is_retained(migrated_postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    try:
        service = NotificationService(
            db, transport=httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200)),
        )
        token = uuid.uuid4().hex
        _seed_sending(service, "delivery-no-file-id", token)
        fd = os.open(service.execution.lock_dir / token, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, b"\0")
        finally:
            os.close(fd)
        report = service.recover_interrupted_sends()
        assert report.recovered == ()
        assert report.retained == (("delivery-no-file-id", "missing-file-identity"),)
        assert _status(db, "delivery-no-file-id") == "SENDING"
        assert seen == []
        assert "notification.unknown" not in _event_types(db)
    finally:
        db.close()


def test_spawn_blocked_send_closes_handles_when_the_gate_does_not_open(tmp_path, monkeypatch):
    gate = tmp_path / "gate-timeout"
    gate.mkdir()
    procs = []
    opened = []
    real_popen = subprocess.Popen
    real_open = Path.open

    def tracking_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        procs.append(proc)
        return proc

    def tracking_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        if self.name in {"replace-out.txt", "replace-err.txt"} and "w" in str(mode):
            opened.append(handle)
        return handle

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)
    monkeypatch.setattr(Path, "open", tracking_open)
    with pytest.raises(AssertionError, match="did not reach the gate"):
        _spawn_blocked_send(
            "postgres://unused", tmp_path / "missing-child", "channel", gate,
            timeout=0.2, command=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
    assert len(procs) == 1 and procs[0].poll() is not None
    assert [handle.closed for handle in opened] == [True, True]
    opened.clear()
    procs.clear()

    def fail_popen(*args, **kwargs):
        raise OSError("popen failed")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    with pytest.raises(OSError, match="popen failed"):
        _spawn_blocked_send(
            "postgres://unused", tmp_path / "popen-child", "channel", gate, timeout=0.2,
        )
    assert [handle.closed for handle in opened] == [True, True]


def _blocked_child_send() -> None:
    gate = Path(os.environ["NOTIFY_GATE_DIR"])

    class Gate(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            (gate / "started").write_text("1", encoding="utf-8")
            while not (gate / "release").exists():
                await asyncio.sleep(0.05)
            return httpx.Response(200, json={"code": 0})

    db = Database(os.environ["DATABASE_URL"], workspace=os.environ["NOTIFY_WORKSPACE"])
    try:
        service = NotificationService(db, transport=Gate())
        asyncio.run(service.send(
            os.environ["NOTIFY_CHANNEL_ID"], "title", "body", "job", owner_id="owner-a",
        ))
    finally:
        db.close()


if __name__ == "__main__" and os.environ.get("NOTIFY_BLOCKED_SEND") == "1":
    _blocked_child_send()
