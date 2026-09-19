import httpx
import pytest


@pytest.mark.asyncio
async def test_single_attempt_prevents_retry_and_sleep(monkeypatch):
    from app.model_gateway import ModelGateway, ModelProfile, ModelRequest, GatewayError
    monkeypatch.setenv("REFERENCE_TEST_KEY", "fake")
    calls, sleeps = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    async def sleep(delay):
        sleeps.append(delay)
    gateway = ModelGateway(ModelProfile("https://example.test/v1", "fake", "REFERENCE_TEST_KEY",
        max_attempts=3, network_retries=3), transport=httpx.MockTransport(handler), sleep=sleep)
    with pytest.raises(GatewayError):
        await gateway.complete(ModelRequest(messages=[], single_attempt=True))
    assert len(calls)==1 and sleeps==[]


@pytest.mark.asyncio
async def test_routed_single_attempt_disables_fallback(tmp_path, monkeypatch):
    from test_routed_model_gateway import _configured_control_plane
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelRequest, GatewayError
    db,bundle,versions = _configured_control_plane(tmp_path,monkeypatch)
    calls=[]
    async def execute(profile, request, **kwargs):
        calls.append(profile.registered_profile_version_id)
        raise GatewayError("unavailable", "server")
    gateway=RoutedModelGateway(db,ModelControlStore(db),execute_attempt=execute)
    with pytest.raises(GatewayError):
        await gateway.complete(ModelRequest(messages=[],role="planner",single_attempt=True),
            context=ModelCallContext("planner","resolve",runtime_bundle_id=bundle.id))
    assert calls==[versions["planner"]]
    with db.connection() as c:
        assert c.execute("SELECT status FROM model_invocations").fetchone()[0]=="FAILED"


@pytest.mark.asyncio
async def test_routed_deadline_releases_invocation(tmp_path,monkeypatch):
    import asyncio
    from test_routed_model_gateway import _configured_control_plane
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelRequest
    db,bundle,_ = _configured_control_plane(tmp_path,monkeypatch)
    async def execute(*args,**kwargs):
        await asyncio.sleep(5)
    gateway=RoutedModelGateway(db,ModelControlStore(db),execute_attempt=execute)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(gateway.complete(ModelRequest(messages=[],role="planner",single_attempt=True),
            context=ModelCallContext("planner","resolve",runtime_bundle_id=bundle.id)),timeout=.05)
    with db.connection() as c:
        assert c.execute("SELECT status FROM model_invocations").fetchone()[0]=="CANCELLED"
