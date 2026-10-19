"""Notification channels and deliveries.

A channel without a stored owner cannot be claimed by the caller. A delivery is
claimed in a transaction before any provider request. Automatic report delivery
does not send again while the same user, report and channel already has a
SENDING, UNKNOWN, or SENT row.

This is not a TaskRuntime worker. The delivery row is the execution right:
the channel lock, the unique dispatch key, and the SENDING status. A restart
marks an interrupted SENDING row UNKNOWN on that same row. It does not insert
another attempt and it does not call the provider.
"""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .policy_adapters import decide_notification
from .policy_engine import PolicyAction
from .research.retriever import validate_public_url

ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


@dataclass(frozen=True)
class NotificationChannel:
    id: str
    name: str
    channel_type: str
    secret_env_name: str
    enabled: bool
    configured: bool
    owner_id: str | None


@dataclass(frozen=True)
class Delivery:
    id: str
    job_id: str | None
    channel_id: str
    attempt: int
    status: str
    http_status: int | None
    provider_code: str | None
    error: str | None
    error_code: str | None = None
    prior_delivery_id: str | None = None


class NotificationService:
    def __init__(self, db, transport=None):
        self.db = db
        self.transport = transport

    def create(self, name, channel_type, secret_env_name, enabled=True, *, owner_id: str):
        if not owner_id or not str(owner_id).strip():
            raise ValueError("notification channel owner is required")
        if channel_type not in {"serverchan", "wecom", "dingtalk", "webhook"} or not ENV_RE.fullmatch(secret_env_name):
            raise ValueError("invalid notification channel")
        now = _now()
        cid = f"channel_{uuid.uuid4().hex}"
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_channels(id,name,channel_type,secret_env_name,enabled,created_at,updated_at,owner_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, name, channel_type, secret_env_name, int(enabled), now, now, owner_id),
            )
        return self.get(cid, owner_id=owner_id)

    def get(self, cid, *, owner_id: str | None = None):
        row = self._channel_row(cid)
        if owner_id is not None and row["owner_id"] != owner_id:
            raise KeyError(cid)
        return _channel(row)

    def list(self, *, owner_id: str):
        with self.db.connection() as connection:
            ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM notification_channels WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at,id",
                    (owner_id,),
                )
            ]
        return [self.get(item, owner_id=owner_id) for item in ids]

    def update(self, cid, *, owner_id: str, name=None, enabled=None, secret_env_name=None):
        current = self.get(cid, owner_id=owner_id)
        env = secret_env_name or current.secret_env_name
        if not ENV_RE.fullmatch(env):
            raise ValueError("invalid notification channel")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE notification_channels SET name=?,secret_env_name=?,enabled=?,updated_at=? WHERE id=? AND owner_id=?",
                (name or current.name, env, int(current.enabled if enabled is None else enabled), _now(), cid, owner_id),
            )
        return self.get(cid, owner_id=owner_id)

    def delete(self, cid, *, owner_id: str):
        """Hide the channel and refuse new sends. Delivery rows stay in place."""
        self.get(cid, owner_id=owner_id)
        now = _now()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE notification_channels SET enabled=0, deleted_at=?, updated_at=? "
                "WHERE id=? AND owner_id=? AND deleted_at IS NULL",
                (now, now, cid, owner_id),
            )

    def history(self, cid, *, owner_id: str) -> list[Delivery]:
        row = self._channel_row(cid, include_deleted=True)
        if row["owner_id"] != owner_id:
            raise KeyError(cid)
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM notification_deliveries WHERE channel_id=? AND owner_id=? ORDER BY created_at,id",
                (cid, owner_id),
            ).fetchall()
        return [_delivery(item) for item in rows]

    async def retry(
        self, job_id, cid, *, owner_id: str, idempotency_key: str,
        confirm_duplicate: bool = False, prior_delivery_id: str | None = None,
    ):
        """Send one explicit retry. The same idempotency key is the same operation."""
        key_text = str(idempotency_key or "").strip()
        if not key_text or len(key_text) > 200:
            raise ValueError("retry idempotency key is required")
        report = self._report(job_id)
        if report is None:
            raise KeyError(job_id)
        channel = self._visible_channel(cid, owner_id)
        source_valid = self._source_valid("report", job_id, owner_id) and bool(channel["enabled"])
        if not _allow("retry", owner_id=owner_id, resource_owner_id=channel["owner_id"], notification_id=cid, source_valid=source_valid):
            return _delivery_view(None, job_id, cid, "FAILED", error="notification was not sent", error_code="NOT_SENT")
        dispatch_key = f"retry:{owner_id}:{job_id}:{cid}:{key_text}"
        claimed = self._claim(
            dispatch_key, channel=channel, job_id=job_id, purpose="report", owner_id=owner_id,
            prior_delivery_id=prior_delivery_id, confirm_duplicate=confirm_duplicate,
        )
        if isinstance(claimed, Delivery):
            return claimed
        if not self._still_allowed(cid, job_id, owner_id, "report", "retry"):
            return self._finish(claimed, "FAILED", error="notification was not sent", error_code="NOT_SENT")
        return await self._post(
            claimed, channel, report["title"] or "Better Agent", report["markdown"] or "", job_id,
            owner_id=owner_id, purpose="report", action="retry",
        )

    async def test(self, cid, *, owner_id: str):
        return await self.send(
            cid, "test", "Better Agent notification test", None,
            owner_id=owner_id, purpose="test", action="send",
            dispatch_key=f"test:{cid}:{uuid.uuid4().hex}",
        )

    async def deliver_completed(self, job_id, title, content):
        owner_id = self._report_owner(job_id)
        if owner_id is None:
            return []
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT s.notify_enabled FROM research_jobs j LEFT JOIN research_schedules s ON s.id=j.schedule_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
        if not row or not row[0]:
            return []
        results = []
        for channel in self.list(owner_id=owner_id):
            if channel.enabled:
                results.append(await self.send(
                    channel.id, title, content[:3000], job_id,
                    owner_id=owner_id, purpose="report", action="send",
                    dispatch_key=f"report:{job_id}:{channel.id}",
                ))
        return results

    async def send(
        self, cid, title, content, job_id, *, owner_id: str, purpose: str = "report",
        action: str = "send", dispatch_key: str | None = None, prior_delivery_id: str | None = None,
    ):
        channel = self._visible_channel(cid, owner_id)
        source_valid = self._source_valid(purpose, job_id, owner_id)
        if not _allow(action, owner_id=owner_id, resource_owner_id=channel["owner_id"], notification_id=cid, source_valid=source_valid and bool(channel["enabled"])):
            return _delivery_view(None, job_id, cid, "FAILED", error="notification was not sent", error_code="NOT_SENT")
        key = dispatch_key or f"report:{job_id}:{cid}"
        claimed = self._claim(
            key, channel=channel, job_id=None if purpose == "test" else job_id, purpose=purpose,
            owner_id=owner_id, prior_delivery_id=prior_delivery_id, automatic=purpose == "report",
        )
        if isinstance(claimed, Delivery):
            return claimed
        if claimed is None:
            return self._stored(key)
        if not self._still_allowed(cid, job_id, owner_id, purpose, action):
            return self._finish(claimed, "FAILED", error="notification was not sent", error_code="NOT_SENT")
        return await self._post(
            claimed, channel, title, content, job_id,
            owner_id=owner_id, purpose=purpose, action=action,
        )

    def _visible_channel(self, cid, owner_id):
        row = self._channel_row(cid)
        if not row["owner_id"] or row["owner_id"] != owner_id:
            raise KeyError(cid)
        return row

    def _source_valid(self, purpose, job_id, owner_id) -> bool:
        if purpose == "test":
            return True
        owner = self._report_owner(job_id)
        return owner is not None and owner == owner_id and self._report(job_id) is not None

    def _still_allowed(self, cid, job_id, owner_id, purpose, action) -> bool:
        try:
            channel = self._visible_channel(cid, owner_id)
        except KeyError:
            return False
        source_valid = self._source_valid(purpose, job_id, owner_id) and bool(channel["enabled"])
        return bool(_allow(action, owner_id=owner_id, resource_owner_id=channel["owner_id"], notification_id=cid, source_valid=source_valid))

    def _claim(
        self, key, *, channel, job_id, purpose, owner_id, prior_delivery_id=None,
        confirm_duplicate=False, automatic=False,
    ):
        """Claim one delivery after the channel lock, in the same transaction.

        Report sends and explicit retries share this check. An automatic send
        never confirms a duplicate: an unresolved or already sent delivery is
        returned as it stands. An explicit retry of an unresolved delivery
        still requires confirmation and that prior id.
        """
        delivery_id = f"delivery_{uuid.uuid4().hex}"
        now = _now()
        try:
            with self.db.transaction() as connection:
                self._lock_channel(connection, channel["id"])
                existing = connection.execute(
                    "SELECT * FROM notification_deliveries WHERE dispatch_key=?", (key,),
                ).fetchone()
                if existing is not None:
                    return _delivery(existing)
                if job_id is not None:
                    blocked = _report_claim_block(
                        connection, job_id=job_id, channel_id=channel["id"], owner_id=owner_id,
                        automatic=automatic, confirm_duplicate=confirm_duplicate,
                        prior_delivery_id=prior_delivery_id,
                    )
                    if blocked is not None:
                        return blocked
                connection.execute(
                    "INSERT INTO notification_deliveries("
                    "id,owner_id,job_id,channel_id,purpose,dispatch_key,attempt,status,created_at,prior_delivery_id) "
                    "VALUES (?,?,?,?,?,?,1,'PENDING',?,?)",
                    (delivery_id, owner_id, job_id, channel["id"], purpose, key, now, prior_delivery_id),
                )
                connection.execute(
                    "UPDATE notification_deliveries SET status='SENDING', claimed_at=? WHERE id=? AND status='PENDING'",
                    (now, delivery_id),
                )
                claimed_row = connection.execute(
                    "SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,),
                ).fetchone()
                self._trace(connection, claimed_row, "notification.claimed")
        except Exception as exc:
            if not _is_unique_conflict(exc):
                raise
            return self._stored(key)
        return delivery_id

    def _lock_channel(self, connection, channel_id):
        lock = " FOR UPDATE" if self.db.backend == "postgresql" else ""
        connection.execute(f"SELECT id FROM notification_channels WHERE id=?{lock}", (channel_id,))

    def _peek(self, key) -> Delivery | None:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM notification_deliveries WHERE dispatch_key=?", (key,)).fetchone()
        return _delivery(row) if row is not None else None

    def _stored(self, key) -> Delivery:
        row = self._peek(key)
        if row is None:
            raise KeyError(key)
        return row

    async def _post(self, delivery_id, channel, title, content, job_id, *, owner_id, purpose, action):
        secret = os.getenv(channel["secret_env_name"])
        try:
            url, kwargs = await self._prepare_request(channel, secret, title, content, job_id)
        except Exception as exc:
            return self._remember(
                delivery_id, "FAILED", error=_redact(exc, secret), error_code="NOT_SENT",
            )
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=10, follow_redirects=False) as client:
                if not self._still_allowed(channel["id"], job_id, owner_id, purpose, action):
                    return self._remember(delivery_id, "FAILED", error="notification was not sent", error_code="NOT_SENT")
                response = await client.post(url, **kwargs)
        except Exception as exc:
            status, error_code = _outcome_after_send_attempt(exc)
            return self._remember(delivery_id, status, error=_redact(exc, secret), error_code=error_code)
        return self._read_provider_result(delivery_id, channel, response, secret)

    async def _prepare_request(self, channel, secret, title, content, job_id):
        """Local checks only. Nothing here has left the process."""
        if not secret:
            raise ValueError("notification secret is not configured")
        url = secret if channel["channel_type"] != "serverchan" else f"https://sctapi.ftqq.com/{secret}.send"
        if channel["channel_type"] != "serverchan" and os.getenv("ALLOW_PRIVATE_WEBHOOKS", "false").lower() != "true":
            url = await validate_public_url(url)
        if channel["channel_type"] == "serverchan":
            kwargs = {"data": {"title": title[:64], "desp": content[:8000]}}
        elif channel["channel_type"] in {"wecom", "dingtalk"}:
            kwargs = {"json": {"msgtype": "markdown", "markdown": {"content": f"{title}\n\n{content}"}}}
        else:
            kwargs = {"json": {"title": title, "content": content, "job_id": job_id, "report_url": f"/api/research/jobs/{job_id}/report"}}
        return url, kwargs

    def _read_provider_result(self, delivery_id, channel, response, secret):
        """The request was submitted. A later read or decode cannot prove it was not received."""
        http_status = response.status_code
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return self._remember(
                delivery_id, "FAILED", http_status=http_status, error=_redact(exc, secret), error_code="REJECTED",
            )
        try:
            data = response.json()
        except ValueError:
            if channel["channel_type"] == "webhook":
                return self._remember(delivery_id, "SENT", http_status=http_status)
            return self._remember(
                delivery_id, "UNKNOWN", http_status=http_status, error="provider response could not be decoded",
                error_code="RESULT_UNKNOWN",
            )
        code = None
        error = None
        provider_ref = None
        status, error_code = "UNKNOWN", "RESULT_UNKNOWN"
        try:
            if not isinstance(data, dict):
                raise _Unconfirmed("provider response is not an object")
            code = _required_success_code(channel["channel_type"], data)
            provider_ref = _provider_ref(data, secret)
            status, error_code = "SENT", None
        except _ProviderRejected as exc:
            status, error_code, error = "FAILED", "REJECTED", _redact(exc, secret)
        except Exception as exc:
            status, error_code, error = "UNKNOWN", "RESULT_UNKNOWN", _redact(exc, secret)
        return self._remember(
            delivery_id, status, http_status=http_status, provider_code=code, error=error,
            error_code=error_code, provider_ref=provider_ref,
        )

    def _remember(self, delivery_id, status, **fields):
        try:
            return self._finish(delivery_id, status, **fields)
        except Exception:
            return self._finish(delivery_id, "UNKNOWN", http_status=fields.get("http_status"), error_code="RESULT_NOT_STORED", error=fields.get("error"))

    def recover_interrupted_sends(self) -> list[Delivery]:
        """Close SENDING rows left by a dead process. Never starts another send."""
        now = _now()
        recovered: list[Delivery] = []
        with self.db.transaction() as connection:
            suffix = " FOR UPDATE" if self.db.backend == "postgresql" else ""
            rows = connection.execute(
                "SELECT * FROM notification_deliveries WHERE status='SENDING' ORDER BY id" + suffix,
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE notification_deliveries SET status='UNKNOWN', error=?, error_code='RESULT_UNKNOWN', finished_at=? "
                    "WHERE id=? AND status='SENDING'",
                    ("send was interrupted before the result was stored", now, row["id"]),
                )
                current = connection.execute(
                    "SELECT * FROM notification_deliveries WHERE id=?", (row["id"],),
                ).fetchone()
                self._trace(connection, current, "notification.unknown")
                recovered.append(_delivery(current))
        return recovered

    def _finish(self, delivery_id, status, *, http_status=None, provider_code=None, error=None, error_code=None, provider_ref=None):
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE notification_deliveries SET status=?,http_status=?,provider_code=?,error=?,error_code=?,provider_ref=?,finished_at=? "
                "WHERE id=? AND status='SENDING'",
                (status, http_status, None if provider_code is None else str(provider_code), error, error_code, provider_ref, _now(), delivery_id),
            ).rowcount
            row = connection.execute("SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,)).fetchone()
            if changed and row is not None:
                self._trace(connection, row, _notification_event(status))
        if row is None:
            raise KeyError(delivery_id)
        return _delivery(row)

    def _trace(self, connection, delivery, event_type: str) -> None:
        """Write the delivery outcome onto the report's existing turn.

        A test send, a missing report, or a deleted thread does not get a
        synthetic turn. The payload has no title, body, or secret.
        """
        job_id = delivery["job_id"]
        if not job_id:
            return
        linked = connection.execute(
            "SELECT j.thread_id, j.source_turn_id FROM research_jobs j "
            "JOIN threads t ON t.id=j.thread_id AND t.owner_id=? AND t.deleted_at IS NULL "
            "JOIN turns tu ON tu.id=j.source_turn_id AND tu.thread_id=j.thread_id "
            "WHERE j.id=?",
            (delivery["owner_id"], job_id),
        ).fetchone()
        if linked is None:
            return
        from .events import ThreadEventStore
        ThreadEventStore(self.db).append(linked["thread_id"], linked["source_turn_id"], event_type, "worker", {
            "delivery_id": delivery["id"], "owner_id": delivery["owner_id"], "job_id": job_id,
            "status": delivery["status"], "purpose": delivery["purpose"], "error_code": delivery["error_code"],
        }, connection=connection)

    def _channel_row(self, cid, *, include_deleted: bool = False):
        with self.db.connection() as connection:
            sql = "SELECT * FROM notification_channels WHERE id=?"
            if not include_deleted:
                sql += " AND deleted_at IS NULL"
            row = connection.execute(sql, (cid,)).fetchone()
        if not row:
            raise KeyError(cid)
        return row

    def _report(self, job_id):
        with self.db.connection() as connection:
            return connection.execute(
                "SELECT title, markdown FROM research_reports WHERE job_id=? AND completed_at IS NOT NULL",
                (job_id,),
            ).fetchone()

    def _report_owner(self, job_id):
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT t.owner_id FROM research_jobs j JOIN threads t ON t.id=j.thread_id "
                "WHERE j.id=? AND t.deleted_at IS NULL AND t.owner_id IS NOT NULL AND trim(t.owner_id)<>''",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return row["owner_id"]


def _report_claim_block(connection, *, job_id, channel_id, owner_id, automatic, confirm_duplicate, prior_delivery_id):
    """Return an existing report delivery that blocks a new claim, or None."""
    rows = connection.execute(
        "SELECT * FROM notification_deliveries WHERE job_id=? AND channel_id=? AND owner_id=? "
        "ORDER BY created_at,id",
        (job_id, channel_id, owner_id),
    ).fetchall()
    unresolved = [row for row in rows if row["status"] in {"SENDING", "UNKNOWN"}]
    if automatic:
        sent = [row for row in rows if row["status"] == "SENT"]
        if sent:
            return _delivery(sent[0])
        if unresolved:
            return _delivery(unresolved[0])
        return None
    unresolved_ids = {row["id"] for row in unresolved}
    if prior_delivery_id and prior_delivery_id not in unresolved_ids:
        raise ValueError("prior delivery is not an unresolved notification for this report")
    if unresolved_ids and (not confirm_duplicate or prior_delivery_id not in unresolved_ids):
        raise ValueError("resending an unresolved notification may duplicate the provider delivery")
    return None


def _allow(action, *, owner_id, resource_owner_id, notification_id, source_valid) -> bool:
    if not resource_owner_id:
        return False
    decision = decide_notification(
        action, owner_id=owner_id, resource_owner_id=resource_owner_id,
        notification_id=notification_id, source_valid=source_valid,
    )
    return decision is not None and decision.action is PolicyAction.ALLOW


class _ProviderRejected(Exception):
    """The provider answered and refused the notification."""


class _Unconfirmed(Exception):
    """A response arrived, but its body does not prove success or rejection."""


def _redact(exc: Exception, secret: str | None) -> str:
    return str(exc)[:300].replace(secret or "", "[REDACTED]")


def _outcome_after_send_attempt(exc: Exception) -> tuple[str, str]:
    """Classify a failure from the send call itself.

    Only a connection error proves the provider was not contacted. Reading,
    decompressing or decoding the response can fail after the request was received.
    """
    if isinstance(exc, httpx.ConnectError):
        return "RETRY_WAIT", "NOT_SENT"
    return "UNKNOWN", "RESULT_UNKNOWN"


def _required_success_code(channel_type: str, data: dict):
    """Return the provider code on success. Missing or ill-typed fields stay unknown."""
    if channel_type == "webhook":
        if "ok" not in data:
            return None
        flag = data["ok"]
        if flag is False:
            raise _ProviderRejected("webhook rejected the notification")
        if flag is True:
            return None
        raise _Unconfirmed("webhook ok flag has an unsupported type")
    field = "code" if channel_type == "serverchan" else "errcode"
    if field not in data or type(data[field]) is not int:
        raise _Unconfirmed(f"{channel_type} response has no integer {field}")
    if data[field] != 0:
        raise _ProviderRejected(f"{channel_type} rejected the notification")
    return data[field]


def _provider_ref(data, secret) -> str | None:
    value = data.get("id") or data.get("msgid")
    if value is None:
        return None
    text = str(value)[:120].replace(secret or "", "[REDACTED]")
    return text or None


def _is_unique_conflict(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return "UNIQUE" in str(exc).upper() or "claim missed" in str(exc)
    return getattr(exc, "sqlstate", None) == "23505"


def _channel(row) -> NotificationChannel:
    return NotificationChannel(
        row["id"], row["name"], row["channel_type"], row["secret_env_name"],
        bool(row["enabled"]), bool(os.getenv(row["secret_env_name"])), row["owner_id"],
    )


def _delivery(row) -> Delivery:
    return Delivery(
        row["id"], row["job_id"], row["channel_id"], int(row["attempt"]), row["status"],
        row["http_status"], row["provider_code"], row["error"], row["error_code"],
        row["prior_delivery_id"],
    )


def _delivery_view(delivery_id, job_id, channel_id, status, *, error, error_code) -> Delivery:
    return Delivery(delivery_id or "", job_id, channel_id, 0, status, None, None, error, error_code)


def _notification_event(status: str) -> str:
    if status == "SENT":
        return "notification.sent"
    if status == "UNKNOWN":
        return "notification.unknown"
    return "notification.failed"


def _now():
    return datetime.now(timezone.utc).isoformat()
