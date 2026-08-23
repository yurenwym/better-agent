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
    channel = service.create("手机", "serverchan", "SERVER_KEY")
    assert channel.configured is True and "secret-send-key" not in repr(channel)
    result = await service.test(channel.id)
    assert result.status == "FAILED"
    assert "secret-send-key" not in (result.error or "")


def test_channel_env_name_is_validated_and_api_shape_masked(tmp_path) -> None:
    service = NotificationService(Database(tmp_path / "agent.db"))
    with pytest.raises(ValueError): service.create("bad", "webhook", "lower-key")
    item = service.create("Hook", "webhook", "MISSING_WEBHOOK")
    assert item.configured is False and item.secret_env_name == "MISSING_WEBHOOK"

@pytest.mark.asyncio
async def test_private_webhook_is_blocked_by_default(tmp_path,monkeypatch):
 monkeypatch.setenv("PRIVATE_HOOK","http://127.0.0.1/hook")
 service=NotificationService(Database(tmp_path/"a.db"),transport=httpx.MockTransport(lambda request:httpx.Response(200)))
 channel=service.create("private","webhook","PRIVATE_HOOK")
 result=await service.test(channel.id)
 assert result.status=="FAILED" and "private" in (result.error or "")
