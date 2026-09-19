"""R1 acceptance: the last gate runs on the exact provider payload (M2-03).

``assert_request_fits`` estimates a request from the canonical
``{"messages", "tools"}`` shape. Every adapter then re-serialises that request
into its own wire format, and those formats are not the same:

* the Anthropic adapter hoists system turns into a top-level ``system`` string
  and rewrites tool definitions into ``{"name","description","input_schema"}``;
* the Gemini adapter builds ``contents`` plus ``functionDeclarations``.

So the estimate alone cannot prove what a provider actually receives. These
tests pin the wire-boundary guarantees:

* the finished payload is measured, not the estimate;
* local ``_context_*`` packing hints never reach a provider body;
* a fallback whose window cannot hold the request is skipped rather than
  allowed to abort a call a later profile could serve;
* a capacity rejection is never resent unchanged.
"""

import asyncio
import json
import os

import httpx
import pytest

from app.model_gateway import ModelGateway, ModelProfile, ModelRequest
from app.token_budget import (
    ContextOverflow,
    assert_provider_payload_fits,
    effective_input_budget,
)

HINTED_MESSAGES = [
    {
        "role": "system",
        "content": "archived episode summary",
        "_context_required": False,
        "_context_priority": 60,
        "_context_group": "memory-context",
    },
    {"role": "user", "content": "hello", "_context_priority": 50},
]


def _profile(**overrides) -> ModelProfile:
    values = {
        "base_url": "https://example.test/v1",
        "model": "demo",
        "api_key_env": "WIRE_KEY",
        "max_attempts": 1,
    }
    values.update(overrides)
    return ModelProfile(**values)


def _capture(profile: ModelProfile, request: ModelRequest, response_body: bytes) -> list[dict]:
    """Send one request through the real adapter and return the outbound bodies."""
    seen: list[dict] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        seen.append(json.loads(incoming.content))
        return httpx.Response(200, content=response_body)

    os.environ["WIRE_KEY"] = "secret"
    gateway = ModelGateway(profile, transport=httpx.MockTransport(handler))
    try:
        asyncio.run(gateway.complete(request))
    except Exception:  # noqa: BLE001 - only the outbound body is under test
        pass
    return seen


# --------------------------------------------------------------------------- #
# M2-03 - the gate measures the finished payload
# --------------------------------------------------------------------------- #


def test_provider_payload_gate_measures_the_finished_body() -> None:
    profile = _profile(context_window=4096, max_output_tokens=1024)
    limit = effective_input_budget(profile).input_limit

    payload = {"model": "demo", "messages": [{"role": "user", "content": "hi"}]}
    expected = len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert assert_provider_payload_fits(payload, profile) == expected

    inflated = {"model": "demo", "messages": [{"role": "user", "content": "x" * (limit + 64)}]}
    with pytest.raises(ContextOverflow, match="provider payload"):
        assert_provider_payload_fits(inflated, profile)


def test_provider_payload_gate_counts_every_top_level_key() -> None:
    """Wrapper keys the estimate never sees still count against the budget."""
    profile = _profile(context_window=4096, max_output_tokens=1024)
    lean = {"messages": [{"role": "user", "content": "hi"}]}
    padded = {**lean, "generationConfig": {"temperature": 0.7, "maxOutputTokens": 512}}

    assert assert_provider_payload_fits(padded, profile) > assert_provider_payload_fits(lean, profile)


# --------------------------------------------------------------------------- #
# M2-03 - packing hints never reach a provider
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "protocol,response_body",
    [
        ("openai_compatible", b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'),
        ("anthropic", b""),
        ("gemini", b""),
    ],
)
def test_no_adapter_sends_local_packing_hints(protocol: str, response_body: bytes) -> None:
    profile = _profile(provider_protocol=protocol)
    seen = _capture(profile, ModelRequest(messages=list(HINTED_MESSAGES)), response_body)

    assert seen, f"the {protocol} adapter must have sent a request"
    serialised = json.dumps(seen[0], ensure_ascii=False)
    for key in ("_context_required", "_context_priority", "_context_group"):
        assert key not in serialised, f"{protocol} leaked {key} to the provider"


def test_openai_adapter_keeps_the_effective_message_content() -> None:
    """Stripping hints must not disturb the messages themselves."""
    profile = _profile()
    seen = _capture(
        profile,
        ModelRequest(messages=list(HINTED_MESSAGES)),
        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
    )

    assert seen[0]["messages"] == [
        {"role": "system", "content": "archived episode summary"},
        {"role": "user", "content": "hello"},
    ]


# --------------------------------------------------------------------------- #
# M2-03 - a capacity rejection is not resent unchanged
# --------------------------------------------------------------------------- #


def test_capacity_rejection_is_not_resent_unchanged() -> None:
    """This layer cannot shrink a payload, so a blind resend is pure waste."""
    calls: list[dict] = []

    def handler(incoming: httpx.Request) -> httpx.Response:
        calls.append(json.loads(incoming.content))
        return httpx.Response(400, content=b'{"error":"maximum context length exceeded"}')

    profile = _profile(max_attempts=3)
    os.environ["WIRE_KEY"] = "secret"
    gateway = ModelGateway(profile, transport=httpx.MockTransport(handler))

    from app.model_gateway import GatewayError

    with pytest.raises(GatewayError) as caught:
        asyncio.run(gateway.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}])))

    assert caught.value.kind == "context_overflow"
    assert len(calls) == 1, "an unchanged body must not be resent after a capacity rejection"


def test_capacity_rejection_records_evidence_invalidation(tmp_path, monkeypatch) -> None:
    db, bundle, versions = _routed_control_plane(tmp_path, monkeypatch, windows={"planner": 16384})

    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError

    async def execute(profile, request, **kwargs):
        raise GatewayError("model context overflow", "context_overflow", 1)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    with pytest.raises(GatewayError) as caught:
        asyncio.run(gateway.complete(
            ModelRequest(messages=[{"role": "planner", "content": "x" * 100}], role="planner"),
            context=ModelCallContext("planner", "plan", runtime_bundle_id=bundle.id),
        ))

    assert caught.value.kind == "context_overflow"
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT event_type FROM model_invocation_events ORDER BY created_at"
        ).fetchall()
    assert "model.context.capacity_evidence_invalidated" in {row["event_type"] for row in rows}


# --------------------------------------------------------------------------- #
# M2-03 - fallback profiles are recomputed and skipped, not fatal
# --------------------------------------------------------------------------- #


def _routed_control_plane(tmp_path, monkeypatch, *, windows: dict[str, int]):
    """A planner chain of three profiles: primary, tiny fallback, roomy fallback."""
    from app.behavior import BehaviorBundleService
    from app.db import Database
    from app.model_admin import ModelAdminService

    db = Database(tmp_path / "agent.db")
    admin = ModelAdminService(db)

    def make(name: str, window: int) -> str:
        env = f"{name.upper()}_KEY"
        monkeypatch.setenv(env, "secret")
        return admin.create_profile({
            "name": name,
            "provider_protocol": "openai_compatible",
            "provider_name": name,
            "base_url": f"https://{name}.test/v1",
            "model_name": name,
            "credential_env_ref": env,
            "capabilities": {"text": True, "streaming": True, "json_object": True},
            "context_window": window,
            "max_output_tokens": min(1024, window // 4),
            "timeout_seconds": 5,
            "max_attempts": 1,
        })["versions"][0]["id"]

    primary = make("planner", windows["planner"])
    tiny = make("tiny", 512)
    roomy = make("roomy", 16384)
    policy = admin.create_policy("runtime", {
        "conversation": {"primary": primary, "fallback": []},
        "planner": {"primary": primary, "fallback": [tiny, roomy]},
    })
    bundle = BehaviorBundleService(db).ensure({
        "model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]},
        "model_role_bindings": policy["roles"],
    })
    BehaviorBundleService(db).activate("stable", bundle.id, "stable")
    return db, bundle, {"primary": primary, "tiny": tiny, "roomy": roomy}


def test_unfittable_fallback_is_skipped_so_a_later_profile_can_serve(tmp_path, monkeypatch) -> None:
    db, bundle, versions = _routed_control_plane(tmp_path, monkeypatch, windows={"planner": 16384})

    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError, ModelResponse, Timing, UsageBuckets

    attempted: list[str] = []

    async def execute(profile, request, **kwargs):
        attempted.append(profile.registered_profile_version_id)
        if profile.registered_profile_version_id == versions["roomy"]:
            return ModelResponse(
                message="served by the roomy fallback", tool_calls=[],
                finish_reason="stop", usage=UsageBuckets(), timing=Timing(0, 0, 0), attempts=1,
            )
        raise GatewayError("primary unavailable", "provider_unavailable", 1)

    gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=execute)
    response = asyncio.run(gateway.complete(
        ModelRequest(messages=[{"role": "planner", "content": "x" * 1000}], role="planner"),
        context=ModelCallContext("planner", "plan", runtime_bundle_id=bundle.id),
    ))

    assert response.message == "served by the roomy fallback"
    assert attempted == [versions["primary"], versions["roomy"]], "the tiny fallback must be skipped"
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT event_type FROM model_invocation_events ORDER BY created_at"
        ).fetchall()
    assert "model.context.fallback_skipped" in {row["event_type"] for row in rows}
