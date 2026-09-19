import pytest

def test_unpriced_model_error_does_not_tell_user_to_retry():
    from app.model_gateway import GatewayError
    error = GatewayError("model price is unavailable", "budget")
    assert "计费价格配置" in error.public_message
    assert "重复发送不会解决" in error.public_message


@pytest.mark.asyncio
async def test_deleted_thread_closes_open_event_stream():
    from types import SimpleNamespace
    from app.api import _thread_event_stream
    class Service:
        events = SimpleNamespace(list=lambda *args: [])
        def thread(self, identity):
            raise KeyError(identity)
    class Request:
        async def is_disconnected(self):
            return False
    assert [item async for item in _thread_event_stream(Service(), "deleted", Request(), 0, True)] == []
