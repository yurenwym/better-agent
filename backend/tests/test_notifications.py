from __future__ import annotations

import os

import httpx
import pytest

from app.db import Database
from app.notifications import NotificationService


@pytest.mark.asyncio
async def test_business_error_is_failure_and_secret_never_returned(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "agent.db")
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 1, "message": "bad"}))
    service = NotificationService(db, transport=transport)
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="local-user")
    assert channel.configured is True and "secret-send-key" not in repr(channel)
    result = await service.test(channel.id, owner_id="local-user")
    assert result.status == "FAILED" and result.error_code == "REJECTED"
    assert "secret-send-key" not in (result.error or "")


def test_channel_env_name_is_validated_and_api_shape_masked(tmp_path) -> None:
    service = NotificationService(Database(tmp_path / "agent.db"))
    with pytest.raises(ValueError): service.create("bad", "webhook", "lower-key", owner_id="local-user")
    item = service.create("Hook", "webhook", "MISSING_WEBHOOK", owner_id="local-user")
    assert item.configured is False and item.secret_env_name == "MISSING_WEBHOOK"

@pytest.mark.asyncio
async def test_private_webhook_is_blocked_by_default(tmp_path,monkeypatch):
 monkeypatch.setenv("PRIVATE_HOOK","http://127.0.0.1/hook")
 service=NotificationService(Database(tmp_path/"a.db"),transport=httpx.MockTransport(lambda request:httpx.Response(200)))
 channel=service.create("private","webhook","PRIVATE_HOOK", owner_id="local-user")
 result=await service.test(channel.id, owner_id="local-user")
 assert result.status=="FAILED" and "private" in (result.error or "")


def _ok(request):
    return httpx.Response(200, json={"code": 0})


def _service(tmp_path, transport):
    return NotificationService(Database(tmp_path / "agent.db"), transport=transport)


def _calls():
    seen = []

    def handler(request):
        seen.append(request)
        return _ok(request)

    return seen, handler


@pytest.mark.asyncio
async def test_cross_owner_and_disabled_channel_send_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen, handler = _calls()
    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    disabled = service.create("停用", "serverchan", "SERVER_KEY", enabled=False, owner_id="owner-a")
    with pytest.raises(KeyError):
        await service.test(channel.id, owner_id="owner-b")
    refused = await service.test(disabled.id, owner_id="owner-a")
    assert refused.status == "FAILED" and refused.error_code == "NOT_SENT"
    assert seen == []
    with service.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_is_not_sent_again(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout("timed out")

    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    first = await service.test(channel.id, owner_id="owner-a")
    assert first.status == "UNKNOWN" and first.error_code == "RESULT_UNKNOWN"
    with service.db.connection() as connection:
        key = connection.execute("SELECT dispatch_key FROM notification_deliveries WHERE id=?", (first.id,)).fetchone()[0]
    again = await service.send(
        channel.id, "test", "again", None, owner_id="owner-a", purpose="test", dispatch_key=key,
    )
    # A new test click uses its own key. Reusing the unknown delivery key must not post again.
    assert again.status == "UNKNOWN"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_connect_failure_is_not_automatically_resent(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ConnectError("refused")

    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    key = f"report:job:{channel.id}"
    _seed_report(service.db, "job", "owner-a")
    first = await service.send(channel.id, "title", "body", "job", owner_id="owner-a", dispatch_key=key)
    assert first.status == "RETRY_WAIT" and first.error_code == "NOT_SENT"
    second = await service.send(channel.id, "title", "body", "job", owner_id="owner-a", dispatch_key=key)
    assert second.status == "RETRY_WAIT"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_explicit_retry_is_a_new_send_after_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout("timed out")
        return _ok(request)

    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    unknown = await service.send(channel.id, "title", "body", "job", owner_id="owner-a", dispatch_key=f"report:job:{channel.id}")
    assert unknown.status == "UNKNOWN"
    retried = await service.retry(
        "job", channel.id, owner_id="owner-a", idempotency_key="send-again",
        confirm_duplicate=True, prior_delivery_id=unknown.id,
    )
    assert retried.status == "SENT" and retried.prior_delivery_id == unknown.id
    assert len(seen) == 2
    with service.db.connection() as connection:
        statuses = {row[0] for row in connection.execute("SELECT status FROM notification_deliveries")}
    assert statuses == {"UNKNOWN", "SENT"}


@pytest.mark.asyncio
async def test_revoked_report_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen, handler = _calls()
    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    sent = await service.send(channel.id, "title", "body", "job", owner_id="owner-a", dispatch_key=f"report:job:{channel.id}")
    assert sent.status == "SENT"
    with service.db.transaction() as connection:
        connection.execute("UPDATE research_reports SET completed_at=NULL WHERE job_id='job'")
    with pytest.raises(KeyError):
        await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="late")
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_one_claim_sends_once(tmp_path, monkeypatch):
    import asyncio

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
    service = _service(tmp_path, gate)
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
    with service.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_disable_after_claim_does_not_post(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    service = _service(tmp_path, httpx.MockTransport(lambda request: seen.append(request) or _ok(request)))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")

    class DisablingClient:
        def __init__(self, *args, **kwargs):
            service.update(channel.id, owner_id="owner-a", enabled=False)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            seen.append(args)
            return _ok(None)

    monkeypatch.setattr(httpx, "AsyncClient", DisablingClient)
    result = await service.test(channel.id, owner_id="owner-a")
    assert result.status == "FAILED" and result.error_code == "NOT_SENT"
    assert seen == []


def test_legacy_pending_is_unknown_and_unowned_channels_stay_unowned(tmp_path):
    import sqlite3

    from app.notification_schema import SQLITE_NOTIFICATION_UPGRADE

    connection = sqlite3.connect(tmp_path / "legacy.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE threads (id TEXT PRIMARY KEY, owner_id TEXT, deleted_at TEXT);
        CREATE TABLE research_jobs (id TEXT PRIMARY KEY, thread_id TEXT);
        CREATE TABLE notification_channels (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, channel_type TEXT NOT NULL, secret_env_name TEXT NOT NULL,
          enabled INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE notification_deliveries (
          id TEXT PRIMARY KEY, job_id TEXT NOT NULL, channel_id TEXT NOT NULL, attempt INTEGER NOT NULL,
          status TEXT NOT NULL, http_status INTEGER, provider_code TEXT, error TEXT,
          created_at TEXT NOT NULL, finished_at TEXT
        );
        """
    )
    connection.execute("INSERT INTO threads(id,owner_id,deleted_at) VALUES ('thr','alice',NULL)")
    connection.execute("INSERT INTO research_jobs(id,thread_id) VALUES ('job','thr'),('orphan',NULL)")
    connection.execute(
        "INSERT INTO notification_channels(id,name,channel_type,secret_env_name,enabled,created_at,updated_at) "
        "VALUES ('ch','old','webhook','HOOK',1,'t','t')"
    )
    connection.execute(
        "INSERT INTO notification_deliveries(id,job_id,channel_id,attempt,status,created_at) VALUES "
        "('pending','job','ch',1,'PENDING','t'),('sent','job','ch',2,'SENT','t'),('loose','orphan','ch',1,'PENDING','t')"
    )
    connection.executescript(SQLITE_NOTIFICATION_UPGRADE)
    channel = connection.execute("SELECT owner_id FROM notification_channels WHERE id='ch'").fetchone()
    rows = {row["id"]: row for row in connection.execute("SELECT id,status,owner_id,purpose,dispatch_key FROM notification_deliveries")}
    assert channel["owner_id"] is None
    assert rows["pending"]["status"] == "UNKNOWN" and rows["pending"]["owner_id"] == "alice"
    assert rows["sent"]["status"] == "SENT"
    assert rows["loose"]["status"] == "UNKNOWN" and rows["loose"]["owner_id"] is None
    assert rows["pending"]["dispatch_key"] == "legacy:pending"
    assert {row["purpose"] for row in rows.values()} == {"report"}


def test_fresh_database_has_delivery_identity(tmp_path):
    db = Database(tmp_path / "fresh.db")
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO notification_channels(id,name,channel_type,secret_env_name,enabled,created_at,updated_at,owner_id) "
            "VALUES ('ch','n','webhook','HOOK',1,'t','t','alice')"
        )
        connection.execute(
            "INSERT INTO notification_deliveries(id,owner_id,channel_id,purpose,dispatch_key,attempt,status,created_at) "
            "VALUES ('d1','alice','ch','test','test:ch:1',1,'UNKNOWN','t')"
        )
    with pytest.raises(Exception):
        with db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries(id,owner_id,channel_id,purpose,dispatch_key,attempt,status,created_at) "
                "VALUES ('d2','alice','ch','test','test:ch:1',1,'PENDING','t')"
            )
    with db.connection() as connection:
        stored = connection.execute("SELECT status FROM notification_deliveries WHERE id='d1'").fetchone()
    assert stored["status"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_non_object_and_invalid_responses_stay_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    bodies = iter((
        httpx.Response(200, json=["accepted"]),
        httpx.Response(200, content=b"not-json"),
    ))
    seen = []

    def handler(request):
        seen.append(request)
        return next(bodies)

    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    array_result = await service.test(channel.id, owner_id="owner-a")
    invalid_result = await service.test(channel.id, owner_id="owner-a")
    assert array_result.status == "UNKNOWN" and array_result.error_code == "RESULT_UNKNOWN"
    assert invalid_result.status == "UNKNOWN" and invalid_result.error_code == "RESULT_UNKNOWN"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_exception_after_the_provider_accepts_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "id": "msg-1"})

    def explode(data, secret):
        raise RuntimeError("after send")

    monkeypatch.setattr("app.notifications._provider_ref", explode)
    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    result = await service.test(channel.id, owner_id="owner-a")
    assert result.status == "UNKNOWN" and result.error_code == "RESULT_UNKNOWN"
    assert result.error_code != "NOT_SENT"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_same_retry_key_sends_once_and_a_new_key_sends_again(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen, handler = _calls()
    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    first = await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="click-1")
    second = await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="click-1")
    third = await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="click-2")
    assert first.id == second.id and third.id != first.id
    assert first.status == "SENT" and third.status == "SENT"
    assert len(seen) == 2
    with service.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_unknown_resend_requires_confirmation_and_keeps_the_original(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout("timed out")
        return _ok(request)

    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    unknown = await service.send(channel.id, "title", "body", "job", owner_id="owner-a", dispatch_key=f"report:job:{channel.id}")
    with pytest.raises(ValueError, match="may duplicate"):
        await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="confirm-me")
    assert len(seen) == 1
    retried = await service.retry(
        "job", channel.id, owner_id="owner-a", idempotency_key="confirm-me",
        confirm_duplicate=True, prior_delivery_id=unknown.id,
    )
    replay = await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="confirm-me")
    assert retried.id == replay.id and retried.prior_delivery_id == unknown.id
    assert retried.status == "SENT" and unknown.status == "UNKNOWN"
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_deleting_a_channel_keeps_inflight_and_unknown_history(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []
    service = _service(tmp_path, httpx.MockTransport(lambda request: seen.append(request) or _ok(request)))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")

    def handler(request):
        seen.append(request)
        service.delete(channel.id, owner_id="owner-a")
        return _ok(request)

    service.transport = httpx.MockTransport(handler)
    sent = await service.test(channel.id, owner_id="owner-a")
    assert sent.status == "SENT"
    assert len(service.history(channel.id, owner_id="owner-a")) == 1
    assert service.list(owner_id="owner-a") == []
    with pytest.raises(KeyError):
        await service.test(channel.id, owner_id="owner-a")

    other = service.create("另一台", "serverchan", "SERVER_KEY", owner_id="owner-a")

    def timeout(request):
        seen.append(request)
        raise httpx.ReadTimeout("timed out")

    service.transport = httpx.MockTransport(timeout)
    unknown = await service.send(
        other.id, "test", "body", None, owner_id="owner-a", purpose="test",
        dispatch_key=f"test:{other.id}:timeout",
    )
    assert unknown.status == "UNKNOWN"
    service.delete(other.id, owner_id="owner-a")
    stored = service.history(other.id, owner_id="owner-a")
    assert [item.status for item in stored] == ["UNKNOWN"]
    with pytest.raises(KeyError):
        await service.test(other.id, owner_id="owner-a")
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_corrupt_gzip_after_accept_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, content=b"not-gzip-bytes",
            headers={"content-encoding": "gzip", "content-type": "application/json"},
        )

    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    result = await service.test(channel.id, owner_id="owner-a")
    assert len(seen) == 1
    assert result.status == "UNKNOWN" and result.error_code == "RESULT_UNKNOWN"
    assert result.error_code != "NOT_SENT"


@pytest.mark.asyncio
async def test_inflight_sending_blocks_an_unconfirmed_retry(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")

    class Gate(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_async_request(self, request):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
            return httpx.Response(200, json={"code": 0})

    gate = Gate()
    service = _service(tmp_path, gate)
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    running = asyncio.create_task(service.retry("job", channel.id, owner_id="owner-a", idempotency_key="first"))
    await gate.started.wait()
    with pytest.raises(ValueError, match="may duplicate"):
        await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="second")
    assert gate.calls == 1
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    with service.db.connection() as connection:
        left = connection.execute("SELECT id,status FROM notification_deliveries").fetchone()
    assert left["status"] == "SENDING"
    with pytest.raises(ValueError, match="may duplicate"):
        await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="third")
    assert gate.calls == 1
    confirmed = await service.retry(
        "job", channel.id, owner_id="owner-a", idempotency_key="confirm-left",
        confirm_duplicate=True, prior_delivery_id=left["id"],
    )
    replay = await service.retry(
        "job", channel.id, owner_id="owner-a", idempotency_key="confirm-left",
        confirm_duplicate=True, prior_delivery_id=left["id"],
    )
    assert confirmed.status == "SENT" and confirmed.prior_delivery_id == left["id"]
    assert replay.id == confirmed.id
    assert gate.calls == 2
    with service.db.connection() as connection:
        original = connection.execute("SELECT status FROM notification_deliveries WHERE id=?", (left["id"],)).fetchone()
    assert original["status"] == "SENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_type", "env_name", "body", "status", "error_code"),
    [
        ("serverchan", "SERVER_KEY", {"code": 0}, "SENT", None),
        ("serverchan", "SERVER_KEY", {"code": 1, "message": "bad"}, "FAILED", "REJECTED"),
        ("serverchan", "SERVER_KEY", {}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("serverchan", "SERVER_KEY", {"message": "ok"}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("serverchan", "SERVER_KEY", {"code": "0"}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("serverchan", "SERVER_KEY", {"code": False}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("wecom", "WECOM_HOOK", {"errcode": 0, "errmsg": "ok"}, "SENT", None),
        ("wecom", "WECOM_HOOK", {"errcode": 93000, "errmsg": "invalid"}, "FAILED", "REJECTED"),
        ("wecom", "WECOM_HOOK", {}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("wecom", "WECOM_HOOK", {"errmsg": "ok"}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("wecom", "WECOM_HOOK", {"errcode": "0"}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("dingtalk", "DING_HOOK", {"errcode": 0, "errmsg": "ok"}, "SENT", None),
        ("dingtalk", "DING_HOOK", {"errcode": 310000, "errmsg": "keywords"}, "FAILED", "REJECTED"),
        ("dingtalk", "DING_HOOK", {}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("dingtalk", "DING_HOOK", {"errcode": 0.0}, "UNKNOWN", "RESULT_UNKNOWN"),
        ("webhook", "HOOK_URL", {}, "SENT", None),
        ("webhook", "HOOK_URL", {"ok": False}, "FAILED", "REJECTED"),
        ("webhook", "HOOK_URL", {"ok": True, "title": "ack"}, "SENT", None),
        ("webhook", "HOOK_URL", {"title": "ack"}, "SENT", None),
        ("webhook", "HOOK_URL", {"ok": "yes"}, "UNKNOWN", "RESULT_UNKNOWN"),
    ],
)
async def test_channel_success_fields(tmp_path, monkeypatch, channel_type, env_name, body, status, error_code):
    async def allow(value, *, resolver=None):
        return value

    monkeypatch.setattr("app.notifications.validate_public_url", allow)
    secret = "https://example.com/hook" if channel_type != "serverchan" else "secret-send-key"
    monkeypatch.setenv(env_name, secret)
    seen = []
    service = _service(tmp_path, httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, json=body)))
    channel = service.create(channel_type, channel_type, env_name, owner_id="owner-a")
    result = await service.test(channel.id, owner_id="owner-a")
    assert len(seen) == 1
    assert result.status == status and result.error_code == error_code


@pytest.mark.asyncio
async def test_webhook_non_json_success_is_not_a_provider_code(tmp_path, monkeypatch):
    async def allow(value, *, resolver=None):
        return value

    monkeypatch.setattr("app.notifications.validate_public_url", allow)
    monkeypatch.setenv("HOOK_URL", "https://example.com/hook")
    seen = []
    service = _service(
        tmp_path,
        httpx.MockTransport(lambda request: seen.append(request) or httpx.Response(200, content=b"accepted")),
    )
    channel = service.create("hook", "webhook", "HOOK_URL", owner_id="owner-a")
    result = await service.test(channel.id, owner_id="owner-a")
    assert len(seen) == 1
    assert result.status == "SENT" and result.error_code is None


def _job_status(db, job_id="job"):
    with db.connection() as connection:
        return connection.execute("SELECT status FROM research_jobs WHERE id=?", (job_id,)).fetchone()["status"]


def _enable_notify(db, job_id):
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO research_schedules(id,name,thread_id,topic,trigger_type,trigger_time,timezone,"
            "notify_enabled,created_at,updated_at) VALUES (?,?,?,?,'daily','09:00','UTC',1,'n','n')",
            (f"sched-{job_id}", "schedule", f"thr-{job_id}", "topic"),
        )
        connection.execute("UPDATE research_jobs SET schedule_id=? WHERE id=?", (f"sched-{job_id}", job_id))


@pytest.mark.asyncio
async def test_automatic_delivery_does_not_send_while_retry_is_sending(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")

    class Gate(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_async_request(self, request):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
            return httpx.Response(200, json={"code": 0})

    gate = Gate()
    service = _service(tmp_path, gate)
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    _enable_notify(service.db, "job")
    running = asyncio.create_task(service.retry("job", channel.id, owner_id="owner-a", idempotency_key="first"))
    await gate.started.wait()
    blocked = await service.deliver_completed("job", "title", "body")
    assert gate.calls == 1
    with service.db.connection() as connection:
        rows = connection.execute("SELECT id,status FROM notification_deliveries").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "SENDING"
    assert blocked[0].id == rows[0]["id"] and blocked[0].status == "SENDING"
    assert _job_status(service.db) == "COMPLETED"
    gate.release.set()
    finished = await running
    assert finished.status == "SENT" and gate.calls == 1


@pytest.mark.asyncio
async def test_automatic_delivery_does_not_resend_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout("timed out")

    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    _enable_notify(service.db, "job")
    unknown = await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="first")
    blocked = await service.deliver_completed("job", "title", "body")
    assert unknown.status == "UNKNOWN" and unknown.error_code == "RESULT_UNKNOWN"
    assert [item.id for item in blocked] == [unknown.id]
    assert blocked[0].status == "UNKNOWN"
    assert len(seen) == 1
    with service.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 1
    assert _job_status(service.db) == "COMPLETED"


@pytest.mark.asyncio
async def test_late_automatic_delivery_does_not_repeat_a_successful_send(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen, handler = _calls()
    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    _enable_notify(service.db, "job")
    sent = await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="first")
    blocked = await service.deliver_completed("job", "title", "body")
    assert sent.status == "SENT"
    assert [item.id for item in blocked] == [sent.id] and blocked[0].status == "SENT"
    assert len(seen) == 1
    with service.db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 1
    assert _job_status(service.db) == "COMPLETED"


@pytest.mark.asyncio
async def test_inflight_automatic_delivery_blocks_an_ordinary_retry(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setenv("SERVER_KEY", "secret-send-key")

    class Gate(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def handle_async_request(self, request):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
            return httpx.Response(200, json={"code": 0})

    gate = Gate()
    service = _service(tmp_path, gate)
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    _enable_notify(service.db, "job")
    running = asyncio.create_task(service.deliver_completed("job", "title", "body"))
    await gate.started.wait()
    with pytest.raises(ValueError, match="may duplicate"):
        await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="other")
    assert gate.calls == 1
    assert _job_status(service.db) == "COMPLETED"
    gate.release.set()
    finished = await running
    assert [item.status for item in finished] == ["SENT"]
    assert gate.calls == 1


@pytest.mark.asyncio
async def test_confirmed_retry_replay_stays_one_extra_send(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_KEY", "secret-send-key")
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout("timed out")
        return _ok(request)

    service = _service(tmp_path, httpx.MockTransport(handler))
    channel = service.create("手机", "serverchan", "SERVER_KEY", owner_id="owner-a")
    _seed_report(service.db, "job", "owner-a")
    _enable_notify(service.db, "job")
    unknown = await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="first")
    with pytest.raises(ValueError, match="may duplicate"):
        await service.retry("job", channel.id, owner_id="owner-a", idempotency_key="confirm-me")
    confirmed = await service.retry(
        "job", channel.id, owner_id="owner-a", idempotency_key="confirm-me",
        confirm_duplicate=True, prior_delivery_id=unknown.id,
    )
    replay = await service.retry(
        "job", channel.id, owner_id="owner-a", idempotency_key="confirm-me",
        confirm_duplicate=True, prior_delivery_id=unknown.id,
    )
    late = await service.deliver_completed("job", "title", "body")
    assert confirmed.id == replay.id and confirmed.prior_delivery_id == unknown.id
    assert confirmed.status == "SENT"
    assert [item.id for item in late] == [confirmed.id]
    assert len(seen) == 2
    with service.db.connection() as connection:
        rows = {row["id"]: row["status"] for row in connection.execute("SELECT id,status FROM notification_deliveries")}
    assert rows == {unknown.id: "UNKNOWN", confirmed.id: "SENT"}
    assert _job_status(service.db) == "COMPLETED"


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
