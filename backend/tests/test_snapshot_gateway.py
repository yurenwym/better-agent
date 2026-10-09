"""G01-G05, G07-G09: the routed gateway consumes only the frozen input.

The gateway is driven with a scripted ``execute_attempt``, so the "network" is a
plain function that can look at the database and at the exact ``ModelRequest`` it
was handed.  No paid model is called.
"""
from __future__ import annotations

import json

import pytest


# --------------------------------------------------------------------------- #
# control plane
# --------------------------------------------------------------------------- #

def _configured_control_plane(
    tmp_path, monkeypatch, *, context_windows=None, max_attempts=None, chained_fallback=False, owner_id="local-user",
    database=None,
):
    """A routed control plane with chat/planner/fallback profiles.

    Mirrors the setup used by ``tests/test_routed_model_gateway.py`` so the two
    files exercise the same routing rules.  ``chained_fallback`` gives the
    planner a second fallback, which is what makes the "skip an unusable
    fallback and let a later one try" branch reachable.
    """
    from app.behavior import BehaviorBundleService
    from app.db import Database
    from app.model_admin import ModelAdminService

    db = database or Database(tmp_path / "agent.db")
    admin = ModelAdminService(db, owner_id=owner_id)
    versions = {}
    context_windows = context_windows or {}
    attempts = max_attempts or {}
    names = ["chat", "planner", "fallback"] + (["fallback2"] if chained_fallback else [])
    for name in names:
        capabilities = {"text": True, "streaming": True} if name == "chat" else {"text": True, "json_object": True, "tool_calling": True}
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
            "max_attempts": attempts.get(name, 1),
        })["versions"][0]["id"]
    planner_fallback = [versions["fallback"]]
    if chained_fallback:
        planner_fallback.append(versions["fallback2"])
    policy = admin.create_policy("runtime", {
        "conversation": {"primary": versions["chat"], "fallback": []},
        **{role: {"primary": versions["planner"], "fallback": planner_fallback}
           for role in ("ask", "executor", "planner", "reflector", "expert", "coordinator", "learning_generator", "learning_judge",
                        "judge_quality", "judge_safety")},
        "researcher": {"primary": versions["chat"], "fallback": []},
    })
    bundles = BehaviorBundleService(db)
    bundle = bundles.ensure({
        "model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]},
        "model_role_bindings": policy["roles"],
    })
    bundles.activate("stable", bundle.id, f"stable:{owner_id}")
    return db, bundle, versions


def _answer(message: str = "好的"):
    from app.model_gateway import ModelResponse, Timing, UsageBuckets

    return ModelResponse(message, [], "stop", UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1)


def _request(messages=None, tools=None, **overrides):
    from app.model_gateway import ModelRequest

    values = {
        "messages": messages or [
            {"role": "system", "content": "你是助手。"},
            {"role": "user", "content": "今天要做什么？"},
        ],
        "tools": tools,
    }
    values.update(overrides)
    return ModelRequest(**values)


def _context(bundle, **overrides):
    from app.model_control import ModelCallContext

    values = {"role": "conversation", "purpose": "route_and_respond", "owner_id": "local-user",
              "runtime_bundle_id": bundle.id}
    values.update(overrides)
    return ModelCallContext(**values)


def _snapshot_row(db, invocation_id):
    with db.connection() as connection:
        return connection.execute(
            "SELECT context_snapshot_id, context_snapshot_digest, request_digest, status "
            "FROM model_invocations WHERE id=?", (invocation_id,),
        ).fetchone()


def _stored_snapshot(db, snapshot_id):
    from app.model_input_snapshot_store import ModelInputSnapshotStore

    return ModelInputSnapshotStore(db).load("local-user", snapshot_id)


def _registered_profile(db, version_id):
    """The full :class:`ModelProfile` the router would build for a version.

    Reusing the router's own row->profile converter keeps the capacity contract
    (admitted/soft limits, counter, protocol budget) identical to production
    instead of hand-rolling a profile that would fail the wire gate for reasons
    unrelated to what a test is checking.
    """
    from app.model_control import RoutedModelGateway

    with db.connection() as connection:
        row = connection.execute(
            "SELECT v.*,p.status AS profile_status FROM model_profile_versions v "
            "JOIN model_profiles p ON p.id=v.profile_id WHERE v.id=?", (version_id,),
        ).fetchone()
    assert row is not None, f"unknown profile version {version_id}"
    return RoutedModelGateway._profile(row)


def _protocol_profile(db, monkeypatch, protocol, *, max_output_tokens=1024, env=None):
    """A registered profile speaking ``protocol``, plus its credential env."""
    from app.model_admin import ModelAdminService

    env = env or f"{protocol.upper()}_KEY"
    monkeypatch.setenv(env, "secret")
    created = ModelAdminService(db).create_profile({
        "name": f"probe-{protocol}",
        "provider_protocol": protocol,
        "provider_name": protocol,
        "base_url": f"https://{protocol}.test/v1",
        "model_name": f"{protocol}-model",
        "credential_env_ref": env,
        "capabilities": {"text": True, "streaming": True},
        "context_window": 16384,
        "max_output_tokens": max_output_tokens,
        "timeout_seconds": 5,
        "max_attempts": 1,
    })
    return _registered_profile(db, created["versions"][0]["id"])


def _counts(db):
    with db.connection() as connection:
        return {
            "invocations": connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0],
            "snapshots": connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0],
            "attempts": connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0],
        }


def _sse(*payloads):
    return "".join(f"data: {json.dumps(item)}\n\n" for item in payloads).encode() + b"data: [DONE]\n\n"


def _openai_stream(text="ok"):
    return _sse({"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]})


def _anthropic_stream(text="ok"):
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
    ]
    return "".join(f"event: message\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def _gemini_stream(text="ok"):
    return json.dumps([
        {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}},
    ]).encode()


_STREAMS = {
    "openai_compatible": _openai_stream,
    "anthropic": _anthropic_stream,
    "gemini": _gemini_stream,
}


# --------------------------------------------------------------------------- #
# G01: the write lands before the request goes out
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_g01_the_snapshot_is_committed_before_the_request_is_sent(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    seen: list = []

    async def execute(profile, request, **_):
        # The "network" reads the ledger: the binding must already be committed.
        with db.connection() as connection:
            row = connection.execute(
                "SELECT id, context_snapshot_id FROM model_invocations "
                "WHERE status='RUNNING' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        seen.append((request, row))
        return _answer()

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    request = _request(tools=[{
        "type": "function",
        "function": {"name": "get_today_tasks", "parameters": {"type": "object", "properties": {}}},
    }])
    await gateway.complete(request, context=_context(bundle))

    sent, row = seen[0]
    assert row is not None and row["context_snapshot_id"]
    stored = _stored_snapshot(db, row["context_snapshot_id"])
    assert stored.content_digest == _snapshot_row(db, row["id"])["context_snapshot_digest"]
    # What was sent is what was frozen.
    assert sent.messages == request.messages
    assert sent.tools == request.tools
    assert stored.to_request().messages == request.messages
    assert stored.to_request().tools == request.tools


# --------------------------------------------------------------------------- #
# G02: mutating the original request after the freeze changes nothing
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_g02_the_frozen_input_survives_mutation_of_the_original_request(
    tmp_path, monkeypatch,
) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    request = _request(tools=[{
        "type": "function",
        "function": {"name": "get_today_tasks", "parameters": {"type": "object", "properties": {}}},
    }])
    sent: list = []

    def on_attempt_started(ordinal, reason):
        # A barrier between the freeze and the send: the caller now vandalises
        # everything it handed over.
        request.messages.append({"role": "user", "content": "污染"})
        request.messages[0]["content"] = "被改写"
        request.tools[0]["function"]["name"] = "renamed"
        request.tools.append({"type": "function", "function": {"name": "extra", "parameters": {}}})
        object.__setattr__(request, "temperature", 0.9)
        object.__setattr__(request, "response_format", {"type": "json_object"})

    async def execute(profile, attempt_request, **_):
        sent.append(attempt_request)
        return _answer()

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    await gateway.complete(request, context=_context(bundle), on_attempt_started=on_attempt_started)

    assert sent[0].messages == [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "今天要做什么？"},
    ]
    assert [tool["function"]["name"] for tool in sent[0].tools] == ["get_today_tasks"]
    assert sent[0].temperature is None
    assert sent[0].response_format is None

    with db.connection() as connection:
        row = connection.execute(
            "SELECT context_snapshot_id FROM model_invocations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    stored = _stored_snapshot(db, row["context_snapshot_id"])
    assert stored.to_request().messages == [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "今天要做什么？"},
    ]


# --------------------------------------------------------------------------- #
# G03/G04/G05: retry, fallback, and a fallback that cannot hold the input
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_g03_a_retry_shares_the_snapshot_and_is_not_polluted(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, max_attempts={"chat": 2})
    attempts: list = []

    async def execute(profile, attempt_request, **_):
        attempts.append(attempt_request)
        if len(attempts) == 1:
            # The first attempt mutates the object it was given and fails.
            attempt_request.messages.append({"role": "user", "content": "第一次的污染"})
            object.__setattr__(attempt_request, "tools", None)
            raise GatewayError("temporary", "timeout")
        return _answer()

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    await gateway.complete(
        _request(tools=[{"type": "function", "function": {"name": "t", "parameters": {}}}]),
        context=_context(bundle),
    )

    assert len(attempts) == 2
    assert attempts[1].messages == [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "今天要做什么？"},
    ]
    assert [tool["function"]["name"] for tool in attempts[1].tools] == ["t"]
    # Both attempts belong to one invocation and therefore one snapshot.
    with db.connection() as connection:
        invocations = connection.execute(
            "SELECT id, context_snapshot_id FROM model_invocations"
        ).fetchall()
        attempt_rows = connection.execute(
            "SELECT invocation_id, ordinal FROM model_attempts ORDER BY ordinal"
        ).fetchall()
        snapshots = connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0]
    assert len(invocations) == 1
    assert len({row["context_snapshot_id"] for row in invocations}) == 1
    assert [row["ordinal"] for row in attempt_rows] == [1, 2]
    assert snapshots == 1


@pytest.mark.asyncio
async def test_g04_fallback_switches_profile_without_changing_the_input(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    attempts: list = []

    async def execute(profile, attempt_request, **_):
        attempts.append((profile.registered_profile_version_id, attempt_request))
        if len(attempts) == 1:
            raise GatewayError("primary is down", "provider_unavailable")
        return _answer("fallback")

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    response = await gateway.complete(
        _request(messages=[{"role": "user", "content": "规划一下"}]),
        context=_context(bundle, role="planner", purpose="plan"),
    )

    assert response.message == "fallback"
    assert len(attempts) == 2
    assert attempts[0][0] != attempts[1][0]
    # One invocation, one snapshot, two attempts — the logical input is identical.
    assert attempts[0][1].messages == attempts[1][1].messages
    assert attempts[0][1] is not attempts[1][1]
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_g05_a_fallback_that_cannot_hold_the_input_is_skipped_not_cropped(
    tmp_path, monkeypatch,
) -> None:
    """The tiny middle fallback is skipped; the last one still gets its chance."""
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError

    db, bundle, versions = _configured_control_plane(
        tmp_path, monkeypatch, context_windows={"fallback": 256}, chained_fallback=True,
    )
    long_text = "很长的上下文。" * 400
    attempts: list = []

    async def execute(profile, attempt_request, **_):
        attempts.append((profile.registered_profile_version_id, attempt_request))
        if len(attempts) == 1:
            raise GatewayError("primary is down", "provider_unavailable")
        return _answer("fallback2")

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    response = await gateway.complete(
        _request(messages=[{"role": "user", "content": long_text}]),
        context=_context(bundle, role="planner", purpose="plan"),
    )

    assert response.message == "fallback2"
    assert [profile for profile, _ in attempts] == [versions["planner"], versions["fallback2"]]
    # The skipped fallback never sent a truncated payload, and the snapshot is
    # the full input — no hidden second version of it was created.
    assert attempts[1][1].messages == [{"role": "user", "content": long_text}]
    with db.connection() as connection:
        skipped = [
            row[0] for row in connection.execute(
                "SELECT event_type FROM model_invocation_events "
                "WHERE event_type='model.context.fallback_skipped'"
            )
        ]
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 2
        row = connection.execute(
            "SELECT context_snapshot_id FROM model_invocations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert skipped == ["model.context.fallback_skipped"]
    assert _stored_snapshot(db, row["context_snapshot_id"]).to_request().messages == [
        {"role": "user", "content": long_text},
    ]


@pytest.mark.asyncio
async def test_g05_when_no_profile_can_hold_the_input_it_fails_closed(
    tmp_path, monkeypatch,
) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError

    db, bundle, _ = _configured_control_plane(
        tmp_path, monkeypatch, context_windows={"fallback": 256},
    )
    long_text = "很长的上下文。" * 400
    attempts: list = []

    async def execute(profile, attempt_request, **_):
        attempts.append(attempt_request)
        raise GatewayError("primary is down", "provider_unavailable")

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(GatewayError) as failure:
        await gateway.complete(
            _request(messages=[{"role": "user", "content": long_text}]),
            context=_context(bundle, role="planner", purpose="plan"),
        )

    assert failure.value.kind == "context_overflow"
    assert len(attempts) == 1
    assert attempts[0].messages == [{"role": "user", "content": long_text}]
    with db.connection() as connection:
        row = connection.execute(
            "SELECT context_snapshot_id FROM model_invocations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 1
    assert _stored_snapshot(db, row["context_snapshot_id"]).to_request().messages == [
        {"role": "user", "content": long_text},
    ]


# --------------------------------------------------------------------------- #
# G07: a persistence failure means zero network, and no fallback escape
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_g07_a_failed_snapshot_write_sends_nothing(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    calls: list = []

    async def execute(profile, attempt_request, **_):
        calls.append(attempt_request)
        return _answer()

    control = ModelControlStore(db)

    def explode(*args, **kwargs):
        raise RuntimeError("simulated snapshot persistence failure")

    monkeypatch.setattr(control.snapshots, "insert", explode)
    gateway = RoutedModelGateway(db, control, execute_attempt=execute)

    with pytest.raises(RuntimeError, match="simulated"):
        await gateway.complete(_request(), context=_context(bundle))

    assert calls == []
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["snapshot", "invocation", "execution_context_binding"])
async def test_t03_any_binding_transaction_write_failure_rolls_back_and_sends_nothing(
    tmp_path, monkeypatch, failure_point,
) -> None:
    from app.execution_context import create_root_context
    from app.harness_context_store import HarnessContextStore
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    harness = create_root_context(owner_id="local-user", runtime_bundle_id=bundle.id)
    context = ModelCallContext.from_harness(harness, role="conversation", purpose="t03_failure_injection")
    calls = []

    async def execute(profile, attempt_request, **_):
        calls.append(attempt_request)
        return _answer()

    control = ModelControlStore(db)
    if failure_point == "snapshot":
        def reject_snapshot(*_args, **_kwargs):
            raise RuntimeError("injected snapshot write failure")
        monkeypatch.setattr(control.snapshots, "insert", reject_snapshot)
    elif failure_point == "invocation":
        with db.transaction() as connection:
            connection.execute("""
                CREATE TRIGGER reject_model_invocation BEFORE INSERT ON model_invocations
                BEGIN SELECT RAISE(FAIL, 'injected invocation write failure'); END
            """)
    else:
        def reject_binding(*_args, **_kwargs):
            raise RuntimeError("injected execution binding write failure")
        monkeypatch.setattr(HarnessContextStore, "save_invocation_context", reject_binding)

    gateway = RoutedModelGateway(db, control, execute_attempt=execute)
    with pytest.raises(Exception, match="injected .* write failure"):
        await gateway.complete(_request(), context=context)

    assert calls == []
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_g07_a_binding_conflict_sends_nothing_and_does_not_fall_back(
    tmp_path, monkeypatch,
) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_input_snapshot import SnapshotBindingConflict

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    calls: list = []

    async def execute(profile, attempt_request, **_):
        calls.append(attempt_request)
        return _answer()

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    # A pre-bound digest with no snapshot behind it is a fabricated binding.
    with pytest.raises(SnapshotBindingConflict):
        await gateway.complete(
            _request(), context=_context(bundle, context_snapshot_digest="0" * 64),
        )

    assert calls == []
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# G08: cancellation
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_g08_a_cancel_after_the_commit_sends_nothing_and_leaves_no_success(
    tmp_path, monkeypatch,
) -> None:
    import asyncio

    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    calls: list = []

    async def execute(profile, attempt_request, **_):
        calls.append(attempt_request)
        return _answer()

    cancel_event = asyncio.Event()
    cancel_event.set()
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(GatewayError) as failure:
        await gateway.complete(_request(), context=_context(bundle), cancel_event=cancel_event)

    assert failure.value.kind == "cancelled"
    assert calls == []
    with db.connection() as connection:
        row = connection.execute(
            "SELECT status, context_snapshot_id FROM model_invocations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        attempts = connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0]
    # The input was frozen and bound, but the call ended cancelled, not succeeded.
    assert row["status"] == "CANCELLED"
    assert row["context_snapshot_id"]
    assert attempts == 0


@pytest.mark.asyncio
async def test_g08_a_cancel_during_an_attempt_leaves_the_snapshot_unchanged(
    tmp_path, monkeypatch,
) -> None:
    import asyncio

    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    cancel_event = asyncio.Event()

    async def execute(profile, attempt_request, **_):
        cancel_event.set()
        raise GatewayError("model request cancelled", "cancelled")

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(GatewayError) as failure:
        await gateway.complete(_request(), context=_context(bundle), cancel_event=cancel_event)

    assert failure.value.kind == "cancelled"
    with db.connection() as connection:
        row = connection.execute(
            "SELECT status, context_snapshot_id FROM model_invocations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        snapshots = connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0]
    assert row["status"] == "CANCELLED"
    assert snapshots == 1
    assert _stored_snapshot(db, row["context_snapshot_id"]).to_request().messages == _request().messages


# --------------------------------------------------------------------------- #
# G09: partial output then failure
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_g09_partial_output_blocks_unsafe_fallback_and_keeps_the_input(
    tmp_path, monkeypatch,
) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    resets: list = []
    attempts: list = []

    async def execute(profile, attempt_request, **kwargs):
        attempts.append(attempt_request)
        kwargs["on_text_delta"]("已经说出了一半")
        raise GatewayError("provider failed mid-stream", "provider_unavailable")

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(GatewayError):
        await gateway.complete(
            _request(messages=[{"role": "user", "content": "规划一下"}]),
            context=_context(bundle, role="planner", purpose="plan"),
            on_text_reset=lambda: resets.append(True),
        )

    # Output had started, so the fallback profile was not used.
    assert len(attempts) == 1
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 1
        row = connection.execute(
            "SELECT context_snapshot_id, status FROM model_invocations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        snapshots = connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0]
    assert row["status"] == "FAILED"
    assert snapshots == 1
    assert _stored_snapshot(db, row["context_snapshot_id"]).to_request().messages == [
        {"role": "user", "content": "规划一下"},
    ]


# --------------------------------------------------------------------------- #
# G06: direct gateway vs routed internal attempt
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_g06_a_direct_gateway_binds_exactly_one_invocation_and_snapshot(
    tmp_path, monkeypatch,
) -> None:
    """A top-level direct call freezes once and binds once."""
    import httpx

    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelGateway, provider_payload

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    profile = _registered_profile(db, versions["chat"])
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_openai_stream())

    store = ModelControlStore(db)
    gateway = ModelGateway(profile, transport=httpx.MockTransport(handler), control_store=store)
    request = _request(tools=[{
        "type": "function",
        "function": {"name": "get_today_tasks", "parameters": {"type": "object", "properties": {}}},
    }])
    await gateway.complete(request, context=_context(bundle))

    assert _counts(db) == {"invocations": 1, "snapshots": 1, "attempts": 1}
    with db.connection() as connection:
        row = connection.execute(
            "SELECT id, context_snapshot_id, context_snapshot_digest, request_digest, status "
            "FROM model_invocations"
        ).fetchone()
    snapshot = _stored_snapshot(db, row["context_snapshot_id"])
    assert row["status"] == "SUCCEEDED"
    assert row["context_snapshot_digest"] == snapshot.content_digest
    # The body on the wire is the adapter's rendering of the *frozen* input.
    assert bodies[0] == provider_payload(profile, snapshot.to_request())
    # The same content frozen twice produces two independent rows.
    from app.model_control import open_model_invocation

    other, _, _ = open_model_invocation(store, profile, request, _context(bundle))
    assert other.input_snapshot_id != row["context_snapshot_id"]
    assert _counts(db)["snapshots"] == 2


@pytest.mark.asyncio
async def test_routed_gateway_refuses_missing_execution_identity_before_any_send(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway, RoutingError

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    sends = []

    async def execute(*_args, **_kwargs):
        sends.append(True)
        return _answer()

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(RoutingError, match="owner"):
        await gateway.complete(_request())
    with pytest.raises(RoutingError, match="owner"):
        await gateway.complete(_request(), context=_context(bundle, owner_id=""))
    assert sends == []
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_direct_gateway_rejects_missing_identity_and_uncontrolled_http(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import GatewayError, ModelGateway

    db = Database(tmp_path / "identity.db")
    profile = _protocol_profile(db, monkeypatch, "openai_compatible")
    direct = ModelGateway(profile, control_store=ModelControlStore(db))
    with pytest.raises(GatewayError, match="owner"):
        await direct.complete(_request(), context=ModelCallContext("conversation", "missing_owner"))
    with pytest.raises(GatewayError, match="control store"):
        await ModelGateway(profile).complete(_request())
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_invocations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_t02_transport_recorder_reads_the_committed_binding_before_send(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    recorder = CommittedSnapshotTransport(
        db=db, owner_id="local-user", callsite_id="CS-CA-01", response=_answer(),
    )
    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=recorder)
    await gateway.complete(_request(), context=_context(bundle))

    assert len(recorder.observations) == 1
    observation = recorder.observations[0]
    assert observation["callsite_id"] == "CS-CA-01"
    assert observation["owner_id"] == "local-user"
    assert observation["invocation_id"] and observation["attempt_id"]
    assert observation["snapshot_id"] and observation["snapshot_digest"]
    assert observation["profile_version_id"]


@pytest.mark.asyncio
async def test_t02_transport_recorder_fails_on_an_unbound_send(tmp_path, monkeypatch) -> None:
    from app.db import Database
    from app.model_gateway import ModelRequest
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport

    recorder = CommittedSnapshotTransport(
        db=Database(tmp_path / "no-binding.db"), owner_id="local-user",
        callsite_id="unmapped", response=None,
    )
    log = tmp_path / "negative-bindings.ndjson"
    monkeypatch.setenv("BETTER_SNAPSHOT_EVIDENCE_PATH", str(log))
    with pytest.raises(AssertionError, match="no committed attempt/invocation"):
        await recorder(None, ModelRequest(messages=[]))
    assert recorder.send_count == 1
    assert json.loads(log.read_text())["valid_committed_binding"] is False


@pytest.mark.asyncio
async def test_g06_the_routed_internal_attempt_adds_no_second_invocation_or_snapshot(
    tmp_path, monkeypatch,
) -> None:
    """Retries inside a routed call stay inside one invocation and one snapshot.

    ``execute_attempt`` mirrors the production ``_execute_http_attempt`` (build a
    ``ModelGateway`` for the attempt profile and call the low-level ``_attempt``)
    so the test proves the *routed* path never re-enters the freezing entry point
    - a retry is an attempt, not a new logical call.
    """
    import httpx

    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError, ModelGateway, provider_payload

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch, max_attempts={"chat": 2})
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503, json={"error": {"message": "flaky"}})
        return httpx.Response(200, content=_openai_stream())

    async def execute(profile, attempt_request, *, cancel_event=None, on_text_delta=None,
                      on_output_started=None):
        gateway = ModelGateway(profile, transport=httpx.MockTransport(handler))
        return await gateway._attempt(
            attempt_request, "secret", cancel_event, on_text_delta, on_output_started,
        )

    store = ModelControlStore(db)
    gateway = RoutedModelGateway(db, store, execute_attempt=execute)
    await gateway.complete(_request(), context=_context(bundle))

    assert _counts(db) == {"invocations": 1, "snapshots": 1, "attempts": 2}
    assert len(bodies) == 2
    with db.connection() as connection:
        snapshot_id = connection.execute(
            "SELECT context_snapshot_id FROM model_invocations"
        ).fetchone()[0]
    snapshot = _stored_snapshot(db, snapshot_id)
    profile = _registered_profile(db, versions["chat"])
    expected = provider_payload(profile, snapshot.to_request())
    # Both attempts sent the same frozen input: the retry reused the snapshot.
    assert bodies[0] == bodies[1] == expected


# --------------------------------------------------------------------------- #
# G10: one frozen input, three protocol renderings
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic", "gemini"])
async def test_g10_the_sent_body_is_the_adapter_output_of_the_frozen_input(
    tmp_path, monkeypatch, protocol,
) -> None:
    """Each adapter's wire body is reproducible from the snapshot alone."""
    import httpx

    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, provider_payload

    db = Database(tmp_path / f"{protocol}.db")
    profile = _protocol_profile(db, monkeypatch, protocol)
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_STREAMS[protocol]())

    store = ModelControlStore(db)
    gateway = ModelGateway(profile, transport=httpx.MockTransport(handler), control_store=store)
    request = _request(
        messages=[{"role": "system", "content": "你是助手。"}, {"role": "user", "content": "今天要做什么？"}],
        tools=[{
            "type": "function",
            "function": {"name": "get_today_tasks", "description": "读取今日任务",
                         "parameters": {"type": "object", "properties": {}}},
        }],
    )
    await gateway.complete(request, context=ModelCallContext(
        role="conversation", purpose="test_protocol_adapter", owner_id="local-user",
    ))

    with db.connection() as connection:
        snapshot_id = connection.execute(
            "SELECT context_snapshot_id FROM model_invocations"
        ).fetchone()[0]
    snapshot = _stored_snapshot(db, snapshot_id)
    frozen = snapshot.to_request()
    # Re-rendering the frozen input with the same adapter reproduces the body
    # byte for byte, which is what makes "what was sent" auditable later.
    assert bodies[0] == provider_payload(profile, frozen)

    if protocol == "anthropic":
        # The adapter hoists system messages out of ``messages``; that rewrite is
        # explainable and must not have touched the frozen logical input.
        assert "system" not in {message["role"] for message in bodies[0]["messages"]}
        assert bodies[0]["system"] == "你是助手。"
        assert frozen.messages[0] == {"role": "system", "content": "你是助手。"}
    if protocol == "gemini":
        assert "contents" in bodies[0] and "messages" not in bodies[0]
        assert bodies[0]["systemInstruction"]["parts"][0]["text"] == "你是助手。"
        assert frozen.messages[0] == {"role": "system", "content": "你是助手。"}
    if protocol == "openai_compatible":
        assert bodies[0]["messages"] == frozen.messages


# --------------------------------------------------------------------------- #
# G11: a null logical max_tokens keeps its profile/protocol resolution local
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_compatible", "anthropic", "gemini"])
async def test_g11_a_null_max_tokens_is_resolved_per_protocol_without_touching_the_snapshot(
    tmp_path, monkeypatch, protocol,
) -> None:
    import httpx

    from app.db import Database
    from app.model_control import ModelCallContext, ModelControlStore
    from app.model_gateway import ModelGateway, provider_payload

    db = Database(tmp_path / f"null-{protocol}.db")
    # A distinctive limit so the assertion can only pass if the resolution came
    # from this profile version rather than from a shared constant.
    profile = _protocol_profile(db, monkeypatch, protocol, max_output_tokens=777)
    bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_STREAMS[protocol]())

    store = ModelControlStore(db)
    gateway = ModelGateway(profile, transport=httpx.MockTransport(handler), control_store=store)
    await gateway.complete(
        _request(max_tokens=None),
        context=ModelCallContext(role="conversation", purpose="test_null_max_tokens", owner_id="local-user"),
    )

    with db.connection() as connection:
        # ``model_invocations`` carries no profile version; the attempt is where
        # the resolved profile version of the actual send is recorded.
        row = connection.execute(
            "SELECT i.id AS invocation_id, i.context_snapshot_id, a.profile_version_id "
            "FROM model_invocations i JOIN model_attempts a ON a.invocation_id=i.id"
        ).fetchone()
        estimated = connection.execute(
            "SELECT data_json FROM model_invocation_events WHERE event_type='model.request.estimated'"
        ).fetchone()
    snapshot = _stored_snapshot(db, row["context_snapshot_id"])
    # 1. The frozen logical input still says null.
    assert snapshot.request_payload()["max_tokens"] is None
    assert snapshot.to_request().max_tokens is None

    # 2. The wire value is the protocol's resolution of that null.
    resolved = bodies[0].get("max_tokens")
    if protocol == "anthropic":
        assert resolved == 777
    elif protocol == "openai_compatible":
        assert "max_tokens" not in bodies[0]
    else:
        assert "maxOutputTokens" not in bodies[0].get("generationConfig", {})

    # 3. The attempt is enough to restore it: the recorded profile version plus
    #    the frozen input reproduce the body, and the event carries the digest.
    recorded = json.loads(estimated["data_json"])
    assert recorded["profile_version_id"] == row["profile_version_id"]
    replayed = provider_payload(_registered_profile(db, row["profile_version_id"]), snapshot.to_request())
    assert replayed == bodies[0]
    import hashlib
    assert recorded["wire_payload_digest"] == hashlib.sha256(
        json.dumps(bodies[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# D01-D07: the send-time asset check runs before every attempt, on both gateways
#
# F01 (independent review, 2026-10-06): the controlled direct gateway froze the
# input and recorded the assets, then went straight to ``start_attempt`` without
# the revocation check the routed gateway performs.  Revoking the bundle after
# the first attempt therefore still produced a second, successful send.  These
# cases pin the check to *every* attempt of *both* gateways.
# --------------------------------------------------------------------------- #

_SSE_OK = (
    b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}'
    b"\n\ndata: [DONE]\n\n"
)


def _learning_assets(db, tmp_path):
    """The real asset service: revoking through it invalidates frozen records."""
    from app.events import EventStore
    from app.learning import LearningService
    from app.memory import MemoryService

    learning = LearningService(db, MemoryService(db, EventStore(db), tmp_path / "memory"), None)
    return learning.assets


def _direct_gateway(db, version_id, *, handler, control):
    """The controlled direct gateway: one profile, one scripted network."""
    from dataclasses import replace

    import httpx

    from app.model_gateway import ModelGateway

    async def no_sleep(_seconds):
        return None

    profile = replace(_registered_profile(db, version_id), retry_base_seconds=0)
    return ModelGateway(
        profile, transport=httpx.MockTransport(handler), sleep=no_sleep,
        control_store=control,
    )


def _invocation_row(db, invocation_id):
    with db.connection() as connection:
        return connection.execute(
            "SELECT status, context_snapshot_id, context_snapshot_digest "
            "FROM model_invocations WHERE id=?", (invocation_id,),
        ).fetchone()


def _attempt_rows(db):
    with db.connection() as connection:
        return connection.execute(
            "SELECT ordinal, status, error_kind FROM model_attempts ORDER BY ordinal"
        ).fetchall()


def _only_invocation_id(db) -> str:
    with db.connection() as connection:
        rows = connection.execute("SELECT id FROM model_invocations").fetchall()
    assert len(rows) == 1, rows
    return rows[0]["id"]


def _snapshot_count(db) -> int:
    with db.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM model_input_snapshots").fetchone()[0]


def _revoking_assets():
    """An asset service whose required check always fails, nothing else."""
    from app.learning_assets import LearningConflict

    class _RevokingAssets:
        def freeze_request(self, connection, context, request, invocation_id):
            return None

        def assert_request_active(self, invocation_id, owner_id):
            raise LearningConflict("request asset snapshot revoked")

    return _RevokingAssets()


@pytest.mark.asyncio
async def test_d01_a_direct_gateway_refuses_the_first_send_after_a_revocation(
    tmp_path, monkeypatch,
) -> None:
    from app.learning_assets import LearningConflict
    from app.model_control import ModelControlStore

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    assets = _learning_assets(db, tmp_path)
    control = ModelControlStore(db)
    control.learning_assets = assets
    sent: list = []

    def handler(request):
        sent.append(request)
        return __import__("httpx").Response(200, content=_SSE_OK)

    gateway = _direct_gateway(db, versions["chat"], handler=handler, control=control)

    def on_attempt_started(ordinal, reason):
        # The asset is revoked after the freeze and after the caller's callback,
        # i.e. immediately before the wire.
        assets.revoke("local-user", "bundle", bundle.id, "revoked_before_send")

    with pytest.raises(LearningConflict):
        await gateway.complete(
            _request(), context=_context(bundle), on_attempt_started=on_attempt_started,
        )

    assert sent == [], "a revoked asset must stop the send before the network"
    row = _invocation_row(db, _only_invocation_id(db))
    assert row["status"] == "FAILED"
    # The frozen input is exactly what it was: a refusal is not a repair.
    stored = _stored_snapshot(db, row["context_snapshot_id"])
    assert stored.content_digest == row["context_snapshot_digest"]
    assert stored.to_request().messages[-1] == {"role": "user", "content": "今天要做什么？"}
    assert _attempt_rows(db) == []


@pytest.mark.asyncio
async def test_d02_a_direct_gateway_does_not_retry_past_a_revocation(
    tmp_path, monkeypatch,
) -> None:
    """F01's original reproduction, as a regression test.

    The first attempt times out and revokes the bundle on its way out; the
    second attempt must never reach the network, and the call must not end
    SUCCEEDED.
    """
    import httpx

    from app.learning_assets import LearningConflict
    from app.model_control import ModelControlStore

    db, bundle, versions = _configured_control_plane(
        tmp_path, monkeypatch, max_attempts={"chat": 2},
    )
    assets = _learning_assets(db, tmp_path)
    control = ModelControlStore(db)
    control.learning_assets = assets
    sent: list = []

    def handler(request):
        sent.append(request)
        if len(sent) == 1:
            assets.revoke("local-user", "bundle", bundle.id, "revoked_between_attempts")
            return httpx.Response(504, json={"error": "upstream"})
        return httpx.Response(200, content=_SSE_OK)

    gateway = _direct_gateway(db, versions["chat"], handler=handler, control=control)

    with pytest.raises(LearningConflict):
        await gateway.complete(_request(), context=_context(bundle))

    assert len(sent) == 1, "the retry must be stopped by the revocation, not sent"
    row = _invocation_row(db, _only_invocation_id(db))
    assert row["status"] == "FAILED", "a revoked asset must never yield SUCCEEDED"


@pytest.mark.asyncio
async def test_d03_a_direct_gateway_retries_normally_while_the_asset_stays_valid(
    tmp_path, monkeypatch,
) -> None:
    """The guard must not become a ban: a valid asset retries as before."""
    import httpx

    from app.model_control import ModelControlStore

    db, bundle, versions = _configured_control_plane(
        tmp_path, monkeypatch, max_attempts={"chat": 2},
    )
    assets = _learning_assets(db, tmp_path)
    control = ModelControlStore(db)
    control.learning_assets = assets
    sent: list = []

    def handler(request):
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(504, json={"error": "upstream"})
        return httpx.Response(200, content=_SSE_OK)

    gateway = _direct_gateway(db, versions["chat"], handler=handler, control=control)
    response = await gateway.complete(_request(), context=_context(bundle))

    assert len(sent) == 2
    assert response.attempts == 2
    row = _invocation_row(db, _only_invocation_id(db))
    assert row["status"] == "SUCCEEDED"
    # One call, one snapshot, and both attempts read the same frozen input.
    assert _snapshot_count(db) == 1
    assert [entry["status"] for entry in _attempt_rows(db)] == ["FAILED", "SUCCEEDED"]
    bodies = {request.content for request in sent}
    assert len(bodies) == 1, "a retry must resend the same frozen input"


@pytest.mark.asyncio
async def test_d04_a_call_without_the_optional_learning_service_still_sends(
    tmp_path, monkeypatch,
) -> None:
    """An installation without the learning service must keep working."""
    import httpx

    from app.model_control import ModelControlStore

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    control = ModelControlStore(db)
    assert getattr(control, "learning_assets", None) is None
    sent: list = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, content=_SSE_OK)

    gateway = _direct_gateway(db, versions["chat"], handler=handler, control=control)
    await gateway.complete(_request(), context=_context(bundle))

    assert len(sent) == 1
    row = _invocation_row(db, _only_invocation_id(db))
    assert row["status"] == "SUCCEEDED"
    assert row["context_snapshot_id"], "the frozen input is saved with or without learning"
    assert _snapshot_count(db) == 1


@pytest.mark.asyncio
async def test_d05_a_failing_required_check_leaves_no_open_attempt_or_reservation(
    tmp_path, monkeypatch,
) -> None:
    from app.learning_assets import LearningConflict
    from app.model_control import ModelControlStore

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    control = ModelControlStore(db)
    control.learning_assets = _revoking_assets()
    sent: list = []

    def handler(request):  # pragma: no cover - the guard must prevent this
        sent.append(request)
        return __import__("httpx").Response(200, content=_SSE_OK)

    gateway = _direct_gateway(db, versions["chat"], handler=handler, control=control)

    with pytest.raises(LearningConflict, match="revoked"):
        await gateway.complete(_request(), context=_context(bundle))

    assert sent == []
    assert _attempt_rows(db) == [], "no attempt may be left STARTED or RUNNING"
    assert _invocation_row(db, _only_invocation_id(db))["status"] == "FAILED"
    # No half-open budget reservation survives a refused call.
    with db.connection() as connection:
        open_roots = connection.execute(
            "SELECT COUNT(*) FROM model_invocations WHERE status='RUNNING'"
        ).fetchone()[0]
    assert open_roots == 0


@pytest.mark.asyncio
async def test_d06_a_callback_that_revokes_is_caught_before_the_wire(
    tmp_path, monkeypatch,
) -> None:
    """The check must not be fooled by a callback that runs after it.

    ``on_attempt_started`` is the last thing a caller controls before the send,
    so a guard that ran *before* it could be defeated by revoking inside it.
    """
    import httpx

    from app.learning_assets import LearningConflict
    from app.model_control import ModelControlStore

    db, bundle, versions = _configured_control_plane(tmp_path, monkeypatch)
    assets = _learning_assets(db, tmp_path)
    control = ModelControlStore(db)
    control.learning_assets = assets
    sent: list = []

    def handler(request):  # pragma: no cover - the guard must prevent this
        sent.append(request)
        return httpx.Response(200, content=_SSE_OK)

    gateway = _direct_gateway(db, versions["chat"], handler=handler, control=control)

    def on_attempt_started(ordinal, reason):
        assets.revoke("local-user", "bundle", bundle.id, "revoked_in_callback")

    with pytest.raises(LearningConflict):
        await gateway.complete(
            _request(), context=_context(bundle), on_attempt_started=on_attempt_started,
        )
    assert sent == []


@pytest.mark.asyncio
async def test_d07_the_routed_gateway_keeps_its_per_attempt_check(tmp_path, monkeypatch) -> None:
    """The shared check must not weaken the routed gateway's existing behaviour."""
    import httpx

    from app.learning_assets import LearningConflict
    from app.model_control import ModelControlStore, RoutedModelGateway

    db, bundle, versions = _configured_control_plane(
        tmp_path, monkeypatch, max_attempts={"chat": 2},
    )
    assets = _learning_assets(db, tmp_path)
    control = ModelControlStore(db)
    control.learning_assets = assets
    sent: list = []

    async def execute(profile, request, **_):
        sent.append(request)
        if len(sent) == 1:
            assets.revoke("local-user", "bundle", bundle.id, "revoked_between_attempts")
            from app.model_gateway import GatewayError

            raise GatewayError("temporary", "timeout")
        return _answer()

    gateway = RoutedModelGateway(db, control, execute_attempt=execute)
    with pytest.raises(LearningConflict):
        await gateway.complete(_request(), context=_context(bundle))

    assert len(sent) == 1
    assert _invocation_row(db, _only_invocation_id(db))["status"] == "FAILED"


# --------------------------------------------------------------------------- #
# D08-D11: a revocation that lands *after* the response
#
# The provider has already run and already billed the attempt when the asset is
# found revoked.  The result may not be used, but the usage is real: dropping it
# would erase a record the invoice will contradict.
# --------------------------------------------------------------------------- #

_METERED_USAGE = (1000, 0, 0, 500, 0)  # uncached in, cache read, cache write, out, reasoning
# price: 1e6 micro-USD per million uncached input tokens, 2e6 per million output
# -> 1000 in * 1e6/1e6 + 500 out * 2e6/1e6 = 2000 micro-USD
_METERED_COST_MICROUSD = 2000


def _metered_response(usage=_METERED_USAGE):
    """A response carrying an explicit usage record.

    ``usage`` is the five-token tuple; ``None`` means the provider returned no
    usage block at all.
    """
    from app.model_gateway import ModelResponse, Timing, UsageBuckets

    buckets = None if usage is None else UsageBuckets(*usage)
    return ModelResponse("好的", [], "stop", buckets, Timing(0, 0, 1), 1)


def _priced_chat_plane(tmp_path, monkeypatch, *, max_attempts=1):
    """A routed control plane with a real price for the chat profile.

    Registering a price is what turns the settled cost into a *real* number
    derived from the reported usage, instead of an unavailable placeholder --
    which is the whole point of the post-response settlement path.
    """
    from app.costs import CostService, PriceSnapshot
    from app.model_control import ModelControlStore

    # These tests assert settlement, not admission limits. Enforcement has its
    # own budget suites and must not be selected by a developer's shell.
    monkeypatch.setenv("BETTER_AGENT_COST_MODE", "observe")

    db, bundle, versions = _configured_control_plane(
        tmp_path, monkeypatch, max_attempts={"chat": max_attempts},
    )
    costs = CostService(db)
    costs.register_price(
        versions["chat"], PriceSnapshot("price-chat", 1_000_000, 0, 0, 2_000_000, 0),
    )
    control = ModelControlStore(db, costs=costs)
    control.learning_assets = _learning_assets(db, tmp_path)
    return db, bundle, versions, control, costs


def _settled_attempt(db, invocation_id, ordinal=1):
    with db.connection() as connection:
        return connection.execute(
            "SELECT status,error_kind,uncached_input_tokens,output_tokens,usage_status,"
            "usage_digest,cost_status,cost_microusd FROM model_attempts WHERE id=?",
            (f"{invocation_id}_attempt_{ordinal}",),
        ).fetchone()


def _invocation_state(db, invocation_id):
    with db.connection() as connection:
        return connection.execute(
            "SELECT status,selected_attempt_id,context_snapshot_id,context_snapshot_digest "
            "FROM model_invocations WHERE id=?", (invocation_id,),
        ).fetchone()


def _charges(db, invocation_id, ordinal=1):
    with db.connection() as connection:
        return connection.execute(
            "SELECT amount_microusd FROM cost_ledger WHERE attempt_id=? AND entry_type='CHARGE'",
            (f"{invocation_id}_attempt_{ordinal}",),
        ).fetchall()


def _attempt_finished_events(db, invocation_id):
    """How many ``model.attempt.finished`` audit events this invocation has.

    Settlement is observable here as well as in the ledger: a second settlement
    of one attempt re-emits these even though the guarded row update is a no-op.
    """
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT data_json FROM model_invocation_events "
            "WHERE invocation_id=? AND event_type='model.attempt.finished'",
            (invocation_id,),
        ).fetchall()
    return [
        json.loads(row["data_json"]) for row in rows
    ]


@pytest.mark.asyncio
async def test_d08_a_revocation_after_the_response_keeps_the_usage_and_settles_it(
    tmp_path, monkeypatch,
) -> None:
    """F04: the attempt really happened, so its usage must not be thrown away.

    The asset is revoked while the response is in flight.  The result is not
    usable and the call fails -- but the provider already ran and already billed
    this attempt, so the usage is persisted and the cost is charged as incurred.
    """
    from app.learning_assets import LearningConflict
    from app.model_control import RoutedModelGateway

    db, bundle, _, control, costs = _priced_chat_plane(tmp_path, monkeypatch)
    assets = control.learning_assets
    sent: list = []

    async def execute(profile, request, **_):
        sent.append(request)
        # In flight: revoked after the send-time check passed, before the
        # response is accepted.
        assets.revoke("local-user", "bundle", bundle.id, "revoked_in_flight")
        return _metered_response()

    gateway = RoutedModelGateway(db, control, execute_attempt=execute)
    with pytest.raises(LearningConflict):
        await gateway.complete(_request(), context=_context(bundle))

    assert len(sent) == 1, "a refusal after the response is not a repair"
    invocation_id = _only_invocation_id(db)
    state = _invocation_state(db, invocation_id)
    assert state["status"] == "FAILED"
    assert state["selected_attempt_id"] is None, "the response must not be selected"

    row = _settled_attempt(db, invocation_id)
    assert (row["status"], row["error_kind"]) == ("FAILED", "asset_revoked")
    # The real usage survives, and is marked as real rather than estimated.
    assert row["uncached_input_tokens"] == _METERED_USAGE[0]
    assert row["output_tokens"] == _METERED_USAGE[3]
    assert row["usage_status"] == "COMPLETE"
    assert row["usage_digest"]
    # ...and it is exactly what the cost was computed from.
    assert (row["cost_status"], row["cost_microusd"]) == ("ESTIMATED_COMPLETE", _METERED_COST_MICROUSD)
    charges = [charge["amount_microusd"] for charge in _charges(db, invocation_id)]
    assert charges and set(charges) == {_METERED_COST_MICROUSD}
    assert costs.summary("local-user", "DAILY", costs.today_period())["charged_microusd"] == _METERED_COST_MICROUSD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage, expected_cost_status",
    [
        (None, "UNAVAILABLE"),
        ((None, None, None, None, None), "ESTIMATED_PARTIAL"),
    ],
    ids=["usage-absent", "usage-partial"],
)
async def test_d09_a_revocation_after_the_response_without_usage_does_not_fabricate_tokens(
    tmp_path, monkeypatch, usage, expected_cost_status,
) -> None:
    """A response that carries no usable usage must not invent one.

    The existing estimate rules still apply: the attempt is recorded, the cost
    stays explicitly partial (or unavailable), and no token counts are made up.
    """
    from app.learning_assets import LearningConflict
    from app.model_control import RoutedModelGateway

    db, bundle, _, control, _ = _priced_chat_plane(tmp_path, monkeypatch)
    assets = control.learning_assets

    async def execute(profile, request, **_):
        assets.revoke("local-user", "bundle", bundle.id, "revoked_in_flight")
        return _metered_response(usage)

    gateway = RoutedModelGateway(db, control, execute_attempt=execute)
    with pytest.raises(LearningConflict):
        await gateway.complete(_request(), context=_context(bundle))

    invocation_id = _only_invocation_id(db)
    row = _settled_attempt(db, invocation_id)
    assert (row["status"], row["error_kind"]) == ("FAILED", "asset_revoked")
    assert row["usage_status"] == "UNAVAILABLE"
    assert row["uncached_input_tokens"] is None
    assert row["output_tokens"] is None
    # No fabricated amount: the cost keeps the existing estimate rule.
    assert row["cost_status"] == expected_cost_status
    assert row["cost_microusd"] is None


@pytest.mark.asyncio
async def test_d10_a_revocation_after_the_response_does_not_retry_or_rewrite_the_snapshot(
    tmp_path, monkeypatch,
) -> None:
    """A refusal is terminal: no retry, no fallback, and the input is untouched."""
    from app.learning_assets import LearningConflict
    from app.model_control import RoutedModelGateway

    # Three attempts are available, so a retry would happen if the code retried.
    db, bundle, _, control, _ = _priced_chat_plane(tmp_path, monkeypatch, max_attempts=3)
    assets = control.learning_assets
    sent: list = []

    async def execute(profile, request, **_):
        sent.append(request)
        assets.revoke("local-user", "bundle", bundle.id, "revoked_in_flight")
        return _metered_response()

    gateway = RoutedModelGateway(db, control, execute_attempt=execute)
    with pytest.raises(LearningConflict):
        await gateway.complete(_request(), context=_context(bundle))

    assert len(sent) == 1, "no retry past a revocation"
    invocation_id = _only_invocation_id(db)
    state = _invocation_state(db, invocation_id)
    assert state["status"] == "FAILED"
    assert state["selected_attempt_id"] is None
    # Exactly one attempt was opened, and it was settled once.
    assert len(_attempt_rows(db)) == 1
    # The frozen input is byte-for-byte what it was: a refusal is not a repair.
    assert _snapshot_count(db) == 1
    stored = _stored_snapshot(db, state["context_snapshot_id"])
    assert stored.content_digest == state["context_snapshot_digest"]
    assert stored.to_request().messages[-1] == {"role": "user", "content": "今天要做什么？"}


@pytest.mark.asyncio
async def test_d11_a_valid_response_after_the_check_still_succeeds_and_settles(
    tmp_path, monkeypatch,
) -> None:
    """The guard must not leak into the success path.

    With the asset still valid the call succeeds, the attempt is selected, and
    the settlement is exactly the metered one.
    """
    from app.model_control import RoutedModelGateway

    db, bundle, _, control, costs = _priced_chat_plane(tmp_path, monkeypatch)
    sent: list = []

    async def execute(profile, request, **_):
        sent.append(request)
        return _metered_response()

    gateway = RoutedModelGateway(db, control, execute_attempt=execute)
    response = await gateway.complete(_request(), context=_context(bundle))

    assert response.message == "好的"
    assert len(sent) == 1
    invocation_id = _only_invocation_id(db)
    state = _invocation_state(db, invocation_id)
    assert state["status"] == "SUCCEEDED"
    assert state["selected_attempt_id"] == f"{invocation_id}_attempt_1"

    row = _settled_attempt(db, invocation_id)
    assert (row["status"], row["error_kind"]) == ("SUCCEEDED", None)
    assert row["usage_status"] == "COMPLETE"
    assert row["uncached_input_tokens"] == _METERED_USAGE[0]
    assert (row["cost_status"], row["cost_microusd"]) == ("ESTIMATED_COMPLETE", _METERED_COST_MICROUSD)
    assert costs.summary("local-user", "DAILY", costs.today_period())["charged_microusd"] == _METERED_COST_MICROUSD


@pytest.mark.asyncio
async def test_d12_a_settled_attempt_cannot_be_settled_a_second_time(
    tmp_path, monkeypatch,
) -> None:
    """Constraint: one attempt is settled exactly once.

    The post-response refusal settles the attempt itself and then re-raises.  If
    that exception were ever caught by the attempt's own error handler, the
    settlement would run a second time.  The ledger itself would not double-charge
    (``_settle`` caches an existing CHARGE per period), but the attempt would be
    announced finished twice and the second announcement would describe a state
    the row no longer has.  The store must refuse the second settlement no matter
    which caller path reaches it.
    """
    from app.learning_assets import LearningConflict
    from app.model_control import ModelCallHandle, RoutedModelGateway

    db, bundle, versions, control, _ = _priced_chat_plane(tmp_path, monkeypatch)
    assets = control.learning_assets

    async def execute(profile, request, **_):
        assets.revoke("local-user", "bundle", bundle.id, "revoked_in_flight")
        return _metered_response()

    gateway = RoutedModelGateway(db, control, execute_attempt=execute)
    with pytest.raises(LearningConflict):
        await gateway.complete(_request(), context=_context(bundle))

    invocation_id = _only_invocation_id(db)
    charges_once = [charge["amount_microusd"] for charge in _charges(db, invocation_id)]
    assert charges_once, "the first settlement must have charged the ledger"
    events_once = _attempt_finished_events(db, invocation_id)
    assert len(events_once) == 1, events_once

    handle = ModelCallHandle(
        invocation_id=invocation_id,
        profile_version_id=versions["chat"],
        # ``begin_invocation`` rebinds the context with the invocation id, and
        # ``finish_attempt`` reads it back from there to write its audit event.
        context=_context(bundle, invocation_id=invocation_id),
    )
    # A second settlement -- whatever the caller's reason -- changes nothing.
    control.finish_attempt(handle, 1, "failed", "asset_revoked", _metered_response())

    assert [charge["amount_microusd"] for charge in _charges(db, invocation_id)] == charges_once
    assert _attempt_finished_events(db, invocation_id) == events_once
    row = _settled_attempt(db, invocation_id)
    assert (row["status"], row["error_kind"]) == ("FAILED", "asset_revoked")
