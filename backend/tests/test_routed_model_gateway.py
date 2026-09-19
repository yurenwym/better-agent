import json

import pytest


def _configured_control_plane(tmp_path, monkeypatch, *, context_windows=None):
    from app.behavior import BehaviorBundleService
    from app.db import Database
    from app.model_admin import ModelAdminService

    db = Database(tmp_path / "agent.db")
    admin = ModelAdminService(db)
    versions = {}
    context_windows = context_windows or {}
    for name, capabilities in {
        "chat": {"text": True, "streaming": True},
        "planner": {"text": True, "json_object": True},
        "fallback": {"text": True, "json_object": True},
    }.items():
        env = f"{name.upper()}_KEY"
        monkeypatch.setenv(env, "secret")
        versions[name] = admin.create_profile({
            "name": name,
            "provider_protocol": "openai_compatible",
            "provider_name": name,
            "base_url": f"https://{name}.test/v1",
            "model_name": name,
            "credential_env_ref": env,
            "capabilities": capabilities,
            "context_window": context_windows.get(name, 16384),
            "max_output_tokens": 1024,
            "timeout_seconds": 5,
            "max_attempts": 1,
        })["versions"][0]["id"]
    policy = admin.create_policy("runtime", {
        "conversation": {"primary": versions["chat"], "fallback": []},
        "planner": {"primary": versions["planner"], "fallback": [versions["fallback"]]},
    })
    bundles = BehaviorBundleService(db)
    bundle = bundles.ensure({
        "model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]},
        "model_role_bindings": policy["roles"],
    })
    bundles.activate("stable", bundle.id, "stable")
    return db, bundle, versions


@pytest.mark.asyncio
async def test_routes_roles_from_the_pinned_runtime_bundle(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelRequest, ModelResponse, Timing, UsageBuckets

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    selected = []

    async def execute(profile, request, **_):
        selected.append(profile.registered_profile_version_id)
        return ModelResponse(profile.model, [], "stop", UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    planner = await gateway.complete(
        ModelRequest(messages=[], role="planner"),
        context=ModelCallContext("planner", "plan", runtime_bundle_id=bundle.id),
    )
    conversation = await gateway.complete(
        ModelRequest(messages=[], role="conversation"),
        context=ModelCallContext("conversation", "answer", runtime_bundle_id=bundle.id),
    )

    assert (planner.message, conversation.message) == ("planner", "chat")
    assert selected == [versions["planner"], versions["chat"]]
    with db.connection() as connection:
        snapshots = [json.loads(row[0]) for row in connection.execute(
            "SELECT route_snapshot_json FROM model_invocations ORDER BY created_at,id"
        )]
    assert snapshots[0]["profile_sequence"] == [versions["planner"], versions["fallback"]]
    assert snapshots[0]["runtime_bundle_id"] == bundle.id


@pytest.mark.asyncio
async def test_conversation_intent_classification_and_answer_use_separate_invocations(
    tmp_path, monkeypatch,
) -> None:
    from app.live_model import LiveConversationModel
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelResponse, Timing, UsageBuckets

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    purposes = []

    async def execute(_profile, request, **kwargs):
        purposes.append(request.purpose)
        if request.purpose == "classify_research_request":
            message = '{"start_research":false,"topic":"制定一个骑行计划"}'
        else:
            message = '{"v":1,"policy":"answer","content_shape":"text","reason_code":"done"}\n请先告诉我你的骑行基础。'
            kwargs["on_text_delta"](message)
        return ModelResponse(
            message, [], "stop", UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1,
        )

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    token = gateway.set_call_context(ModelCallContext(
        "conversation",
        "route_and_respond",
        runtime_bundle_id=bundle.id,
        invocation_id="conversation:turn-cycling",
    ))
    try:
        response = await LiveConversationModel(gateway).route_and_respond(
            content="请调查骑行训练方案是否适合新手",
            history=[],
            skill_names=[],
            on_text_delta=lambda _chunk: None,
            on_text_reset=lambda: None,
            cancel_event=None,
        )
    finally:
        gateway.reset_call_context(token)

    assert "骑行基础" in response.message
    assert purposes == ["classify_research_request", "route_and_respond"]
    with db.connection() as connection:
        invocations = connection.execute(
            "SELECT id,purpose,status FROM model_invocations ORDER BY created_at,id"
        ).fetchall()
    assert [tuple(row) for row in invocations] == [
        (
            "conversation:turn-cycling:classify_research_request",
            "classify_research_request",
            "SUCCEEDED",
        ),
        ("conversation:turn-cycling", "route_and_respond", "SUCCEEDED"),
    ]


@pytest.mark.asyncio
async def test_all_frozen_roles_enter_the_unified_invocation_ledger(tmp_path, monkeypatch) -> None:
    from app.behavior import BehaviorBundleService
    from app.db import Database
    from app.model_admin import ModelAdminService, ROLES
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelRequest, ModelResponse, Timing, UsageBuckets

    db=Database(tmp_path/"roles.db");admin=ModelAdminService(db);monkeypatch.setenv("ALL_ROLES_KEY","secret")
    version=admin.create_profile({"name":"all-roles","provider_protocol":"openai_compatible","provider_name":"test",
        "base_url":"https://roles.test/v1","model_name":"all","credential_env_ref":"ALL_ROLES_KEY",
        "capabilities":{"text":True,"streaming":True,"tool_calling":True,"json_object":True},
        "context_window":8192,"max_output_tokens":1024,"timeout_seconds":5,"max_attempts":1})["versions"][0]["id"]
    policy=admin.create_policy("all-roles",{role:{"primary":version,"fallback":[]} for role in ROLES})
    bundles=BehaviorBundleService(db);bundle=bundles.ensure({"model_routing":{"policy_id":policy["id"],"digest":policy["policy_digest"]},"model_role_bindings":policy["roles"]})
    async def execute(profile,request,**kwargs):return ModelResponse("ok",[],"stop",UsageBuckets(1,0,0,1,0),Timing(0,0,1),1)
    gateway=RoutedModelGateway(db,ModelControlStore(db),execute_attempt=execute)
    for role in sorted(ROLES):
        await gateway.complete(ModelRequest(messages=[],role=role),context=ModelCallContext(role,"role-contract",runtime_bundle_id=bundle.id))
    with db.connection() as connection:
        rows=connection.execute("SELECT role,runtime_bundle_id,status FROM model_invocations ORDER BY role").fetchall()
    assert {row["role"] for row in rows}==ROLES
    assert all(row["runtime_bundle_id"]==bundle.id and row["status"]=="SUCCEEDED" for row in rows)


@pytest.mark.asyncio
async def test_explicit_fallback_is_one_invocation_with_separate_attempts(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError, ModelRequest, ModelResponse, Timing, UsageBuckets

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    with db.transaction() as connection:
        connection.execute("INSERT INTO threads(id,title,created_at,updated_at) VALUES ('thread-route','路由','now','now')")
        connection.execute("INSERT INTO turns(id,thread_id,client_turn_id,status,version,runtime_bundle_id,created_at,updated_at) VALUES ('turn-route','thread-route','route','ROUTING',0,?,'now','now')", (bundle.id,))

    async def execute(profile, request, **_):
        if profile.registered_profile_version_id == versions["planner"]:
            raise GatewayError("offline", "provider_unavailable", 1)
        return ModelResponse("fallback", [], "stop", UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1)

    result = await RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute).complete(
        ModelRequest(messages=[], role="planner"),
        context=ModelCallContext("planner", "plan", thread_id="thread-route", turn_id="turn-route", runtime_bundle_id=bundle.id),
    )

    assert result.message == "fallback"
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 1
        attempts = connection.execute(
            "SELECT ordinal,reason,profile_version_id,status,error_kind FROM model_attempts ORDER BY ordinal"
        ).fetchall()
    assert [tuple(row) for row in attempts] == [
        (1, "primary", versions["planner"], "FAILED", "provider_unavailable"),
        (2, "fallback", versions["fallback"], "SUCCEEDED", None),
    ]
    with db.connection() as connection:
        thread_events = [row[0] for row in connection.execute(
            "SELECT type FROM thread_events WHERE thread_id='thread-route' ORDER BY seq"
        )]
    assert "model.fallback.selected" in thread_events


@pytest.mark.asyncio
@pytest.mark.parametrize("started_kind", ["text", "tool_call"])
async def test_never_falls_back_after_any_output_started(tmp_path, monkeypatch, started_kind) -> None:
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError, ModelRequest

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    calls = []

    async def execute(profile, request, *, on_text_delta=None, on_output_started=None, **_):
        calls.append(profile.registered_profile_version_id)
        if started_kind == "text":
            on_text_delta("partial")
        else:
            on_output_started("tool_call")
        raise GatewayError("late failure", "server", 1)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(GatewayError, match="late failure"):
        await gateway.complete(
            ModelRequest(messages=[], role="planner"),
            context=ModelCallContext("planner", "plan", runtime_bundle_id=bundle.id),
        )
    assert calls == [versions["planner"]]
    if started_kind == "tool_call":
        with db.connection() as connection:
            attempt = connection.execute("SELECT first_token_at FROM model_attempts ORDER BY ordinal LIMIT 1").fetchone()
        assert attempt["first_token_at"] is not None


@pytest.mark.asyncio
async def test_disabled_primary_routes_to_explicit_eligible_fallback(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway, RoutingError
    from app.model_gateway import ModelRequest

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    called = []

    async def execute(*args, **kwargs):
        from app.model_gateway import ModelResponse, Timing, UsageBuckets
        called.append(args[0].registered_profile_version_id)
        return ModelResponse("fallback", [], "stop", UsageBuckets(1,0,0,1,0), Timing(0,0,1), 1)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with db.transaction() as connection:
        connection.execute(
            "UPDATE model_profile_versions SET status='DISABLED' WHERE id=(SELECT json_extract(roles_json,'$.planner.primary') FROM model_routing_policies LIMIT 1)"
        )
    result = await gateway.complete(
        ModelRequest(messages=[], role="planner"),
        context=ModelCallContext("planner", "plan", runtime_bundle_id=bundle.id),
    )
    assert result.message == "fallback"
    assert called == [_["fallback"]]


@pytest.mark.asyncio
async def test_run_and_ask_continuation_keep_their_creation_bundle(tmp_path, monkeypatch) -> None:
    from app.startup import build_runtime

    monkeypatch.setenv("AGENT_MODEL_API_KEY", "secret")
    monkeypatch.setenv("AGENT_MODEL_BASE_URL", "https://model.test/v1")
    monkeypatch.setenv("AGENT_MODEL_ID", "demo")
    monkeypatch.setenv("AGENT_MODEL_CAPABILITIES", "streaming,tool_calling,json_object")
    runtime = build_runtime(tmp_path)
    original = runtime.behavior.active("stable")

    run = await runtime.create_goal("固定配置", "验证在途任务")
    thread = runtime.conversation.create_thread("固定对话")
    first = runtime.conversation.accept_turn(thread.id, "first", "制定训练计划", [])
    with runtime.db.transaction() as connection:
        connection.execute("UPDATE turns SET status='AWAITING_INPUT',version=1 WHERE id=?", (first.turn_id,))
        connection.execute(
            "INSERT INTO turn_asks(id,turn_id,call_id,questions_json,status,created_at) VALUES ('ask-pin',?,'call-pin',?,'PENDING','now')",
            (first.turn_id, json.dumps([{"id":"goal","header":"目标","question":"目标？","options":[],"multi_select":False,"allow_free_text":True}])),
        )

    changed = runtime.behavior.ensure({**original.manifest, "prompts": "changed"})
    runtime.behavior.activate("stable", changed.id, "changed-stable")
    continuation = runtime.conversation.answer_ask(
        first.turn_id, 1, "answer-pin", [{"question_id":"goal","selected_options":[],"free_text":"提高耐力"}]
    ).turn
    new_run = await runtime.create_goal("新配置", "只供新任务")

    assert run.runtime_bundle_id == original.id
    assert runtime.conversation.turn(first.turn_id).runtime_bundle_id == original.id
    assert continuation.runtime_bundle_id == original.id
    assert new_run.runtime_bundle_id == changed.id


@pytest.mark.asyncio
async def test_routed_budget_block_is_explainable_and_happens_before_transport(tmp_path, monkeypatch) -> None:
    from app.costs import CostService, PriceSnapshot
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError, ModelRequest

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    costs = CostService(db)
    costs.register_price(versions["planner"], PriceSnapshot("planner-price", 1_000_000,0,0,1_000_000,0))
    costs.set_budget("local-user", "DAILY", costs.today_period(), 0)
    called = False
    async def execute(*args, **kwargs):
        nonlocal called
        called = True
    gateway = RoutedModelGateway(db, ModelControlStore(db, costs=costs), execute_attempt=execute)
    with pytest.raises(GatewayError) as caught:
        await gateway.complete(ModelRequest(messages=[], role="planner"), context=ModelCallContext("planner","plan",runtime_bundle_id=bundle.id))
    assert caught.value.kind == "budget"
    assert called is False


@pytest.mark.asyncio
async def test_routed_context_overflow_before_invocation_does_not_call_transport(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError, ModelRequest

    db, bundle, versions = _configured_control_plane(
        tmp_path, monkeypatch, context_windows={"planner": 512}
    )
    called = False

    async def execute(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("context overflow must be rejected before transport")

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(GatewayError) as caught:
        await gateway.complete(
            ModelRequest(messages=[{"role": "planner", "content": "x" * 1000}], role="planner"),
            context=ModelCallContext("planner", "plan", runtime_bundle_id=bundle.id),
        )

    assert caught.value.kind == "context_overflow"
    assert caught.value.attempts == 0
    assert called is False
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_routed_fallback_context_overflow_fails_existing_invocation_without_fallback_attempt(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError, ModelRequest

    db, bundle, versions = _configured_control_plane(
        tmp_path, monkeypatch, context_windows={"fallback": 512}
    )
    called = []

    async def execute(profile, request, **kwargs):
        called.append(profile.registered_profile_version_id)
        raise GatewayError("primary unavailable", "provider_unavailable", 1)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(GatewayError) as caught:
        await gateway.complete(
            ModelRequest(messages=[{"role": "planner", "content": "x" * 1000}], role="planner"),
            context=ModelCallContext("planner", "plan", runtime_bundle_id=bundle.id),
        )

    assert caught.value.kind == "context_overflow"
    assert caught.value.attempts == 1
    assert called == [versions["planner"]]
    with db.connection() as connection:
        invocation = connection.execute("SELECT status FROM model_invocations").fetchone()
        attempts = connection.execute(
            "SELECT ordinal,profile_version_id,status,error_kind FROM model_attempts ORDER BY ordinal"
        ).fetchall()
    assert invocation["status"] == "FAILED"
    assert [tuple(row) for row in attempts] == [
        (1, versions["planner"], "FAILED", "provider_unavailable"),
    ]
