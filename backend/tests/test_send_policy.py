import asyncio

import httpx
import pytest

from app.db import Database
from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest
from test_snapshot_gateway import _configured_control_plane
from app.send_authority import send_authority


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("cancel_at", [1, 2])
async def test_cancellation_after_callback_blocks_actual_send(tmp_path, monkeypatch, routed, cancel_at):
    sends = []
    cancel = asyncio.Event()
    if routed:
        db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, max_attempts={"chat": 2})
        async def send(*args, **kwargs):
            sends.append("sent")
            raise GatewayError("retry", "server")
        gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=send)
        context = ModelCallContext("conversation", "answer", owner_id="local-user", runtime_bundle_id=bundle.id)
    else:
        db = Database(tmp_path / "direct.db")
        monkeypatch.setenv("POLICY_KEY", "test")
        def send(request):
            sends.append("sent")
            return httpx.Response(500, json={"error": "retry"})
        gateway = ModelGateway(ModelProfile("https://offline.test", "offline", "POLICY_KEY", max_attempts=2),
                               control_store=ModelControlStore(db), transport=httpx.MockTransport(send))
        context = ModelCallContext("conversation", "answer", owner_id="local-user")
    try:
        def started(attempt, reason):
            if attempt == cancel_at:
                cancel.set()
        with pytest.raises(GatewayError) as caught:
            await gateway.complete(ModelRequest([{"role": "user", "content": "hello"}]), context=context,
                                   cancel_event=cancel, on_attempt_started=started)
        assert caught.value.kind == "cancelled"
        assert len(sends) == cancel_at - 1
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == cancel_at - 1
            assert connection.execute("SELECT status FROM model_invocations").fetchone()[0] == "CANCELLED"
    finally:
        db.close()


@pytest.mark.parametrize("source", ["assets", "learning", "evolution"])
def test_source_refusal_preserves_domain_error(tmp_path, source):
    from types import SimpleNamespace
    from app.learning import LearningConflict
    from app.evolution import EvolutionGateError
    db = Database(tmp_path / "source.db")
    try:
        store = ModelControlStore(db)
        error = EvolutionGateError("revoked") if source == "evolution" else LearningConflict("revoked")
        def refuse(*args):
            raise error
        if source == "assets":
            store.learning_assets = SimpleNamespace(assert_request_active=refuse)
        else:
            store.learning = SimpleNamespace(assert_pinned_prompt_active=refuse)
        with pytest.raises(type(error)) as caught:
            store.assert_request_active("invocation", ModelCallContext("conversation", "answer",
                                        owner_id="local-user", runtime_bundle_id="bundle"))
        assert caught.value is error
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("lost_at", [1, 2])
async def test_execution_right_is_refreshed_after_callback_on_every_attempt(tmp_path, monkeypatch, routed, lost_at):
    sends = []
    active = True
    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, max_attempts={"chat": 2})
    store = ModelControlStore(db)
    if routed:
        async def send(*args, **kwargs):
            sends.append(1)
            raise GatewayError("retry", "server")
        gateway = RoutedModelGateway(db, store, execute_attempt=send)
    else:
        def send(request):
            sends.append(1)
            return httpx.Response(500, json={"error": "retry"})
        gateway = ModelGateway(ModelProfile("https://offline.test", "offline", "CHAT_KEY", max_attempts=2),
                               control_store=store, transport=httpx.MockTransport(send))
    def check():
        if not active:
            raise PermissionError("lease lost")
    def started(attempt, reason):
        nonlocal active
        active = attempt != lost_at
    try:
        with send_authority(check), pytest.raises(PermissionError, match="lease lost"):
            await gateway.complete(ModelRequest([{"role": "user", "content": "hello"}]),
                context=ModelCallContext("conversation", "answer", owner_id="local-user", runtime_bundle_id=bundle.id),
                on_attempt_started=started)
        assert len(sends) == lost_at - 1
        with db.connection() as connection:
            assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == lost_at - 1
        # Task-local guard is reset even when a refusal interrupts execution.
        store.assert_request_active("unrelated", ModelCallContext("conversation", "answer"))
    finally:
        db.close()
