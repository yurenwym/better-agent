"""U01-U10: the shared execution context, its factories and its adapters.

These are unit tests.  A stubbed gateway proves *propagation*; it is not a
quality evaluation of any model.
"""
from __future__ import annotations

import json

import pytest

from app.execution_context import (
    CONTEXT_FIELDS,
    HarnessContextError,
    UnknownSchemaVersion,
    canonical_json,
    context_envelope,
    create_child_context,
    create_root_context,
    deserialize_context,
    execution_context_digest,
    serialize_context,
)


# --------------------------------------------------------------------------- #
# U01-U06: the context itself
# --------------------------------------------------------------------------- #

def test_u01_two_roots_have_distinct_trace_and_span_and_no_parent() -> None:
    first = create_root_context(owner_id="owner-a")
    second = create_root_context(owner_id="owner-a")
    assert first.trace_id != second.trace_id
    assert first.span_id != second.span_id
    assert first.parent_span_id is None and second.parent_span_id is None
    assert first.owner_id == second.owner_id == "owner-a"


def test_u02_child_and_grandchild_inherit_business_identity() -> None:
    root = create_root_context(
        owner_id="owner-a", thread_id="thread-1", turn_id="turn-1", run_id="chat-turn:turn-1",
        project_id="project-1", root_budget_id="budget-1", runtime_bundle_id="bundle-1",
    )
    child = create_child_context(root)
    grandchild = create_child_context(child)
    for derived in (child, grandchild):
        assert derived.owner_id == root.owner_id
        assert derived.trace_id == root.trace_id
        assert derived.root_budget_id == root.root_budget_id
        assert derived.runtime_bundle_id == root.runtime_bundle_id
        assert derived.root_task_id == root.root_task_id
        assert derived.project_id == root.project_id
    assert {root.span_id, child.span_id, grandchild.span_id}.__len__() == 3
    assert child.parent_span_id == root.span_id
    assert grandchild.parent_span_id == child.span_id


def test_u03_plain_conversation_has_no_task_and_creates_none() -> None:
    root = create_root_context(owner_id="local-user", thread_id="thread-1", turn_id="turn-1")
    child = create_child_context(root)
    for derived in (root, child):
        assert derived.task_id is None
        assert derived.parent_task_id is None
        assert derived.root_task_id is None


def test_u04_root_task_is_the_root_and_children_keep_the_task_tree() -> None:
    root = create_root_context(owner_id="owner-a", task_id="task-root")
    assert root.root_task_id == "task-root"
    assert root.parent_task_id is None
    child = create_child_context(root)
    assert (child.task_id, child.parent_task_id, child.root_task_id) == (
        "task-root", None, "task-root",
    )


def test_u05_empty_owner_overrides_and_malformed_ids_are_rejected() -> None:
    with pytest.raises(HarnessContextError):
        create_root_context(owner_id="")
    with pytest.raises(HarnessContextError):
        create_root_context(owner_id=None)  # type: ignore[arg-type]
    # There is no overrides hook: identity cannot be injected from outside.
    with pytest.raises(TypeError):
        create_root_context(owner_id="owner-a", **{"trace_id": "f" * 32})
    with pytest.raises(TypeError):
        create_root_context(owner_id="owner-a", **{"span_id": "f" * 32})
    root = create_root_context(owner_id="owner-a")
    with pytest.raises(HarnessContextError):
        create_child_context(root).__class__(  # type: ignore[misc]
            owner_id="owner-a", trace_id=root.trace_id, span_id="not a span",
        )


def test_u06_serialisation_round_trip_and_digest_sensitivity() -> None:
    root = create_root_context(
        owner_id="owner-a", thread_id="thread-1", turn_id="turn-1", root_budget_id="budget-1",
    )
    payload = serialize_context(root)
    restored = deserialize_context(payload)
    assert restored == root
    assert restored.span_id == root.span_id
    assert deserialize_context(json.loads(payload)) == root

    baseline = execution_context_digest(payload)
    # Whitespace and key order must not change the digest.
    assert execution_context_digest(json.dumps(json.loads(payload), indent=2)) == baseline
    assert execution_context_digest(json.dumps(json.loads(payload), sort_keys=False)) == baseline

    # Any field value must.
    envelope = json.loads(payload)
    envelope["context"]["thread_id"] = "thread-2"
    assert execution_context_digest(envelope) != baseline
    envelope = json.loads(payload)
    envelope["context"]["root_budget_id"] = None
    assert execution_context_digest(envelope) != baseline

    unknown = json.loads(payload)
    unknown["schema_version"] = "harness-execution-context-v99"
    with pytest.raises(UnknownSchemaVersion):
        deserialize_context(unknown)

    injected = json.loads(payload)
    injected["context"]["is_admin"] = True
    with pytest.raises(HarnessContextError):
        deserialize_context(injected)

    missing = json.loads(payload)
    missing["context"].pop("root_budget_id")
    with pytest.raises(HarnessContextError):
        deserialize_context(missing)

    assert set(json.loads(payload)["context"]) == set(CONTEXT_FIELDS)
    assert json.loads(canonical_json(context_envelope(root)))["schema_version"]


# --------------------------------------------------------------------------- #
# U07-U08: the type adapters
# --------------------------------------------------------------------------- #

def test_u07_conversion_keeps_span_and_specialised_fields() -> None:
    from app.model_control import ModelCallContext
    from app.tools import ToolExecutionContext

    harness = create_child_context(create_root_context(
        owner_id="owner-a", thread_id="thread-1", turn_id="turn-1", run_id="chat-turn:turn-1",
        project_id="project-1", root_budget_id="budget-1", runtime_bundle_id="bundle-1",
        task_id="task-1",
    ))
    model = ModelCallContext.from_harness(
        harness, role="conversation", purpose="route_and_respond", invocation_id="conversation:turn-1",
    )
    tool = ToolExecutionContext.from_harness(harness, tool_call_id="call-1")

    for derived in (model, tool):
        assert derived.trace_id == harness.trace_id
        assert derived.span_id == harness.span_id
        assert derived.parent_span_id == harness.parent_span_id
        assert derived.owner_id == harness.owner_id
        assert derived.root_budget_id == harness.root_budget_id
        assert derived.runtime_bundle_id == harness.runtime_bundle_id
        assert derived.turn_id == harness.turn_id

    assert (model.role, model.purpose) == ("conversation", "route_and_respond")
    assert model.invocation_id == "conversation:turn-1"
    assert model.agent_task_id == "task-1"
    assert tool.tool_call_id == "call-1"
    assert tool.run_id == "chat-turn:turn-1"


def test_u08_conversion_refuses_identity_overrides() -> None:
    from app.model_control import ModelCallContext
    from app.tools import ToolExecutionContext

    harness = create_root_context(owner_id="owner-a", run_id="chat-turn:turn-1")
    for forbidden in (
        {"owner_id": "owner-b"}, {"root_budget_id": "budget-2"},
        {"runtime_bundle_id": "bundle-2"}, {"trace_id": "a" * 32}, {"span_id": "b" * 32},
    ):
        with pytest.raises(TypeError):
            ModelCallContext.from_harness(
                harness, role="conversation", purpose="route_and_respond", **forbidden,
            )
        with pytest.raises(TypeError):
            ToolExecutionContext.from_harness(harness, tool_call_id="call-1", **forbidden)

    # A context that was converted from a harness cannot be re-pointed at
    # another identity afterwards either.
    from dataclasses import replace

    from app.execution_context import ContextIdentityConflict

    converted = ModelCallContext.from_harness(
        harness, role="conversation", purpose="route_and_respond",
    )
    with pytest.raises(ContextIdentityConflict):
        replace(converted, owner_id="owner-b")
    # A tool call without a run binding is refused, not silently defaulted.
    with pytest.raises(HarnessContextError):
        ToolExecutionContext.from_harness(
            create_root_context(owner_id="owner-a"), tool_call_id="call-1",
        )


# --------------------------------------------------------------------------- #
# U09-U10: gateway wrappers
# --------------------------------------------------------------------------- #

def _configured_control_plane(tmp_path, monkeypatch):
    from app.behavior import BehaviorBundleService
    from app.db import Database
    from app.model_admin import ModelAdminService

    db = Database(tmp_path / "agent.db")
    admin = ModelAdminService(db)
    versions = {}
    for name, capabilities in {
        "chat": {"text": True, "streaming": True},
        "planner": {"text": True, "json_object": True},
    }.items():
        env = f"{name.upper()}_KEY"
        monkeypatch.setenv(env, "secret")
        versions[name] = admin.create_profile({
            "name": name, "provider_protocol": "openai_compatible", "provider_name": name,
            "base_url": f"https://{name}.test/v1", "model_name": name,
            "credential_env_ref": env, "capabilities": capabilities,
            "context_window": 16384, "max_output_tokens": 1024, "timeout_seconds": 5,
            "max_attempts": 2,
        })["versions"][0]["id"]
    policy = admin.create_policy("runtime", {
        "conversation": {"primary": versions["chat"], "fallback": []},
        "planner": {"primary": versions["planner"], "fallback": []},
    })
    bundles = BehaviorBundleService(db)
    bundle = bundles.ensure({
        "model_routing": {"policy_id": policy["id"], "digest": policy["policy_digest"]},
        "model_role_bindings": policy["roles"],
    })
    bundles.activate("stable", bundle.id, "stable")
    return db, bundle, versions


class _RecordingStore:
    """Records the context every invocation was opened with."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.contexts: list[object] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def begin_invocation(self, profile, request, context, route_snapshot=None, **kwargs):
        # ``**kwargs`` on purpose: the store gained a ``snapshot`` keyword in
        # phase 2 and this recorder only cares about the context.  Passing them
        # through keeps the double honest instead of silently dropping a
        # binding the caller asked for.
        self.contexts.append(context)
        return self._inner.begin_invocation(profile, request, context, route_snapshot, **kwargs)


@pytest.mark.asyncio
async def test_u09_independent_calls_get_new_spans_and_retries_do_not(
    tmp_path, monkeypatch,
) -> None:
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import GatewayError, ModelRequest, ModelResponse, Timing, UsageBuckets

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    store = _RecordingStore(ModelControlStore(db))
    attempts: list[int] = []

    async def execute(_profile, _request, **_):
        attempts.append(1)
        if len(attempts) == 1:
            raise GatewayError("flaky", "server")
        return ModelResponse("ok", [], "stop", UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1)

    gateway = RoutedModelGateway(db, store, execute_attempt=execute)
    turn = create_root_context(owner_id="local-user", runtime_bundle_id=bundle.id)

    first = create_child_context(turn)
    second = create_child_context(turn)
    assert first.span_id != second.span_id
    assert first.parent_span_id == second.parent_span_id == turn.span_id

    from app.model_control import ModelCallContext

    await gateway.complete(
        ModelRequest(messages=[], role="conversation"),
        context=ModelCallContext.from_harness(first, role="conversation", purpose="route_and_respond"),
    )
    assert len(attempts) == 2  # one retry inside a single logical invocation
    assert len(store.contexts) == 1
    # The retry reused the logical span instead of minting a second one.
    assert store.contexts[0].span_id == first.span_id
    assert store.contexts[0].trace_id == turn.trace_id

    await gateway.complete(
        ModelRequest(messages=[], role="conversation"),
        context=ModelCallContext.from_harness(second, role="conversation", purpose="route_and_respond"),
    )
    assert len(store.contexts) == 2
    assert store.contexts[1].span_id == second.span_id
    assert store.contexts[1].span_id != store.contexts[0].span_id


@pytest.mark.asyncio
async def test_u10_gateway_wrappers_keep_the_public_identity(tmp_path, monkeypatch) -> None:
    from app.model_control import ModelCallContext, ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelRequest, ModelResponse, Timing, UsageBuckets

    db, bundle, _ = _configured_control_plane(tmp_path, monkeypatch)
    store = _RecordingStore(ModelControlStore(db))

    async def execute(profile, _request, **_):
        return ModelResponse(profile.model, [], "stop", UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1)

    gateway = RoutedModelGateway(db, store, execute_attempt=execute)
    turn = create_root_context(owner_id="local-user", root_budget_id="budget-1", runtime_bundle_id=bundle.id)
    llm = create_child_context(turn)
    model = ModelCallContext.from_harness(llm, role="conversation", purpose="route_and_respond")

    # Same logical call twice (a wrapper re-entering the gateway) keeps one span.
    for _ in range(2):
        await gateway.complete(ModelRequest(messages=[], role="conversation"), context=model)
    assert len(store.contexts) == 2
    assert {ctx.span_id for ctx in store.contexts} == {llm.span_id}
    assert {ctx.trace_id for ctx in store.contexts} == {turn.trace_id}
    # The route preserves the version already pinned by the trusted root.
    for ctx in store.contexts:
        assert ctx.runtime_bundle_id == bundle.id
        assert ctx.harness is not None and ctx.harness.runtime_bundle_id == bundle.id
        assert ctx.root_budget_id == "budget-1"

    # Legacy callers that pass no harness keep working unchanged.
    await gateway.complete(
        ModelRequest(messages=[], role="conversation"),
        context=ModelCallContext("conversation", "answer", owner_id="local-user", runtime_bundle_id=bundle.id),
    )
    assert store.contexts[-1].harness is None
    assert store.contexts[-1].span_id is None


@pytest.mark.parametrize("field", ["owner_id", "run_id", "thread_id", "turn_id", "project_id",
    "agent_task_id", "task_id", "parent_task_id", "root_task_id", "root_budget_id",
    "runtime_bundle_id", "trace_id", "span_id", "parent_span_id"])
def test_adapters_reject_all_duplicate_identity_conflicts(field):
    from dataclasses import replace
    from app.execution_context import ContextIdentityConflict, rebind
    from app.model_control import ModelCallContext
    from app.tools import ToolExecutionContext
    harness = create_root_context(owner_id="owner", run_id="run")
    contexts = [ModelCallContext.from_harness(harness, role="conversation", purpose="test"),
                ToolExecutionContext.from_harness(harness, tool_call_id="call")]
    for context in contexts:
        if not hasattr(context, field):
            continue
        with pytest.raises(ContextIdentityConflict):
            replace(context, **{field: "f" * 32})
        with pytest.raises(ContextIdentityConflict):
            rebind(context, **{field: "f" * 32})
    assert rebind(contexts[0], purpose="updated").harness == harness


def test_rebind_cannot_replace_pinned_or_null_bundle():
    from app.execution_context import ContextIdentityConflict, rebind
    from app.model_control import ModelCallContext
    for bundle in (None, "original"):
        context = ModelCallContext.from_harness(
            create_root_context(owner_id="owner", runtime_bundle_id=bundle), role="planner", purpose="test")
        with pytest.raises(ContextIdentityConflict):
            rebind(context, runtime_bundle_id="replacement")
        assert rebind(context, runtime_bundle_id=bundle).harness == context.harness
