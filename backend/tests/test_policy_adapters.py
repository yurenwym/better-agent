"""P01: adapters take resource identity and security facts explicitly."""
import pytest

from app.policy_adapters import (
    asset_promote_request,
    asset_read_request,
    asset_write_request,
    model_send_request,
    notification_retry_request,
    notification_send_request,
    task_cancel_request,
    task_claim_request,
    task_commit_request,
    task_heartbeat_request,
    task_query_request,
    tool_execute_request,
    tool_resume_request,
)
from app.policy_engine import ActionKind, PolicyAction, PolicyPhase, decide_request


_SECURITY = dict(
    registered=True, allowed=True, identity_valid=True, source_valid=True,
    budget_available=True, execution_valid=True, cancelled=False,
)


def _model(**overrides):
    fields = dict(owner_id="owner", resource_owner_id="owner", phase=PolicyPhase.SEND, invocation_id="inv", **_SECURITY)
    fields.update(overrides)
    return model_send_request(**fields)


def _tool(**overrides):
    fields = dict(
        owner_id="owner", resource_owner_id="owner", tool_name="read_note", call_id="c", **_SECURITY,
    )
    fields.update(overrides)
    return tool_execute_request(**fields)


def test_model_send_request_carries_caller_and_resource_identity():
    request = _model(phase=PolicyPhase.RETRY, thread_id="thr", turn_id="turn", resource_thread_id="thr")
    assert request.action is ActionKind.MODEL_SEND and request.phase is PolicyPhase.RETRY
    assert request.subject.owner_id == "owner" and request.subject.thread_id == "thr"
    assert request.resource.id == "inv" and request.resource.owner_id == "owner"
    assert decide_request(request).action is PolicyAction.ALLOW


def test_model_send_request_does_not_copy_the_subject_onto_the_resource():
    request = _model(
        owner_id="caller", resource_owner_id="resource-owner",
        thread_id="caller-thread", resource_thread_id="resource-thread",
    )
    assert request.subject.owner_id == "caller"
    assert request.resource.owner_id == "resource-owner"
    assert request.resource.thread_id == "resource-thread"
    with pytest.raises(ValueError, match="owner"):
        decide_request(request)


def test_model_send_request_rejects_a_cross_thread_resource():
    request = _model(thread_id="thread", resource_thread_id="elsewhere")
    with pytest.raises(ValueError, match="thread"):
        decide_request(request)


def test_model_send_request_requires_security_facts():
    with pytest.raises(TypeError):
        model_send_request(
            owner_id="o", resource_owner_id="o", phase=PolicyPhase.SEND, invocation_id="i",
        )


def test_model_send_request_rejects_an_illegal_phase():
    request = _model(phase=PolicyPhase.CLAIM)
    with pytest.raises(ValueError, match="phase"):
        decide_request(request)


def test_model_send_request_refuses_a_revoked_source_or_lost_lease():
    revoked = _model(owner_id="o", resource_owner_id="o", invocation_id="i", source_valid=False)
    assert decide_request(revoked).reason.value == "SOURCE_INVALID"
    lost = _model(owner_id="o", resource_owner_id="o", invocation_id="i", execution_valid=False)
    assert decide_request(lost).reason.value == "EXECUTION_RIGHT_LOST"


def test_tool_execute_request_uses_the_callers_identity_and_an_explicit_resource():
    request = _tool(thread_id="thr", resource_thread_id="thr")
    assert request.action is ActionKind.TOOL_EXECUTE and request.resource.kind == "tool"
    assert request.resource.owner_id == "owner" and request.resource.thread_id == "thr"
    assert decide_request(request).action is PolicyAction.ALLOW


def test_tool_execute_request_rejects_another_owners_call():
    request = _tool(owner_id="owner", resource_owner_id="other")
    with pytest.raises(ValueError, match="owner"):
        decide_request(request)


def test_tool_resume_request_uses_the_commit_phase():
    request = tool_resume_request(
        owner_id="owner", resource_owner_id="owner", tool_name="write_note", call_id="c", **_SECURITY,
    )
    assert request.phase is PolicyPhase.COMMIT
    assert decide_request(request).action is PolicyAction.ALLOW


# Resource owner is passed explicitly.  Matching the subject is a test choice,
# not something the adapter fills in.
_ADAPTERS = [
    (lambda: _model(owner_id="o", resource_owner_id="o", invocation_id="i"), ActionKind.MODEL_SEND),
    (lambda: _tool(owner_id="o", resource_owner_id="o", tool_name="t"), ActionKind.TOOL_EXECUTE),
    (lambda: tool_resume_request(owner_id="o", resource_owner_id="o", tool_name="t", call_id="c", **_SECURITY), ActionKind.TOOL_EXECUTE),
    (lambda: task_query_request(owner_id="o", resource_owner_id="o", task_id="j", task_kind="research", **_SECURITY), ActionKind.TASK_QUERY),
    (lambda: task_claim_request(owner_id="o", resource_owner_id="o", task_id="j", task_kind="research", **_SECURITY), ActionKind.TASK_CLAIM),
    (lambda: task_heartbeat_request(owner_id="o", resource_owner_id="o", task_id="j", task_kind="research", **_SECURITY), ActionKind.TASK_HEARTBEAT),
    (lambda: task_commit_request(owner_id="o", resource_owner_id="o", task_id="j", task_kind="research", **_SECURITY), ActionKind.TASK_COMMIT),
    (lambda: task_cancel_request(owner_id="o", resource_owner_id="o", task_id="j", task_kind="research", **_SECURITY), ActionKind.TASK_CANCEL),
    (lambda: asset_read_request(owner_id="o", resource_owner_id="o", asset_id="a", **_SECURITY, approval_required=False, approval_granted=False), ActionKind.ASSET_READ),
    (lambda: asset_write_request(owner_id="o", resource_owner_id="o", asset_id="a", **_SECURITY, approval_required=False, approval_granted=False), ActionKind.ASSET_WRITE),
    (lambda: asset_promote_request(owner_id="o", resource_owner_id="o", asset_id="a", **_SECURITY, approval_required=True, approval_granted=True), ActionKind.ASSET_PROMOTE),
    (lambda: notification_send_request(owner_id="o", resource_owner_id="o", notification_id="n", **_SECURITY, approval_required=False, approval_granted=False), ActionKind.NOTIFICATION_SEND),
    (lambda: notification_retry_request(owner_id="o", resource_owner_id="o", notification_id="n", **_SECURITY, approval_required=False, approval_granted=False), ActionKind.NOTIFICATION_RETRY),
]


@pytest.mark.parametrize("build,action", _ADAPTERS)
def test_every_adapter_builds_a_decidable_request(build, action):
    request = build()
    assert request.action is action
    assert request.resource.owner_id == "o"
    assert decide_request(request).action is PolicyAction.ALLOW


@pytest.mark.parametrize("build,action", _ADAPTERS)
def test_approval_alone_does_not_override_a_refusal(build, action):
    """A granted approval cannot bypass identity/source/budget."""
    request = build()
    from dataclasses import replace
    from app.policy_engine import VerifiedFacts
    refused = replace(request, facts=VerifiedFacts(
        registered=True, allowed=True, identity_valid=False,
        approval_required=True, approval_granted=True, source_valid=False,
    ))
    decision = decide_request(refused)
    assert decision.action is PolicyAction.DENY and decision.reason.value == "IDENTITY_INVALID"


def test_notification_request_rejects_unknown_facts():
    with pytest.raises(TypeError):
        notification_send_request(
            owner_id="o", resource_owner_id="o", notification_id="n", invented=True,
            **_SECURITY, approval_required=False, approval_granted=False,
        )


def test_asset_request_requires_explicit_security_facts():
    with pytest.raises(TypeError):
        asset_read_request(owner_id="o", resource_owner_id="o", asset_id="a")


def test_asset_promote_refuses_a_revoked_source():
    facts = dict(_SECURITY, source_valid=False)
    request = asset_promote_request(
        owner_id="o", resource_owner_id="o", asset_id="a", **facts,
        approval_required=True, approval_granted=True,
    )
    decision = decide_request(request)
    assert decision.action is PolicyAction.DENY and decision.reason.value == "SOURCE_INVALID"
