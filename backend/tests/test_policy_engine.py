import pytest

from app.policy_engine import (
    ActionKind,
    PolicyAction,
    PolicyInput,
    PolicyPhase,
    PolicyReason,
    PolicyRequest,
    ResourceRef,
    SubjectRef,
    VerifiedFacts,
    decide,
    decide_request,
)


@pytest.mark.parametrize("facts,action,reason", [
    (PolicyInput(True, True, True), PolicyAction.ALLOW, PolicyReason.ALLOWED),
    (PolicyInput(False, False, False, True), PolicyAction.DENY, PolicyReason.UNKNOWN_CAPABILITY),
    (PolicyInput(True, False, True, True, True), PolicyAction.DENY, PolicyReason.CAPABILITY_NOT_ALLOWED),
    (PolicyInput(True, True, False, True, True), PolicyAction.DENY, PolicyReason.IDENTITY_INVALID),
    (PolicyInput(True, True, True, True), PolicyAction.REQUIRE_APPROVAL, PolicyReason.APPROVAL_REQUIRED),
    (PolicyInput(True, True, True, True, True), PolicyAction.ALLOW, PolicyReason.ALLOWED),
    (PolicyInput(True, True, True, True, cancelled=True), PolicyAction.DENY, PolicyReason.CANCELLED),
    (PolicyInput(True, True, True, True, source_valid=False), PolicyAction.DENY, PolicyReason.SOURCE_INVALID),
    (PolicyInput(True, True, True, True, budget_available=False), PolicyAction.DENY, PolicyReason.BUDGET_EXHAUSTED),
    (PolicyInput(True, True, True, True, execution_valid=False), PolicyAction.DENY, PolicyReason.EXECUTION_RIGHT_LOST),
])
def test_deterministic_decision_and_deny_precedence(facts, action, reason):
    decision = decide(facts)
    assert decision == decide(facts)
    assert (decision.action, decision.reason) == (action, reason)


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_unverified_facts_cannot_be_used_as_authorization(value):
    with pytest.raises(ValueError):
        PolicyInput(True, value, True)


def _request(**facts):
    return PolicyRequest(
        subject=SubjectRef(owner_id="owner", thread_id="thread", turn_id="turn"),
        action=ActionKind.MODEL_SEND,
        resource=ResourceRef(kind="model_invocation", id="invocation", owner_id="owner"),
        phase=PolicyPhase.RETRY,
        facts=VerifiedFacts(registered=True, allowed=True, identity_valid=True, **facts),
    )


@pytest.mark.parametrize("facts,reason", [
    ({"execution_valid": False}, PolicyReason.EXECUTION_RIGHT_LOST),
    ({"source_valid": False}, PolicyReason.SOURCE_INVALID),
    ({"budget_available": False}, PolicyReason.BUDGET_EXHAUSTED),
    ({"cancelled": True}, PolicyReason.CANCELLED),
])
def test_request_denies_lost_lease_revoked_source_budget_and_cancel(facts, reason):
    decision = decide_request(_request(**facts))
    assert (decision.action, decision.reason) == (PolicyAction.DENY, reason)


def test_request_is_pure_and_keeps_action_phase():
    request = _request()
    assert decide_request(request) == decide_request(request)
    assert request.action is ActionKind.MODEL_SEND and request.phase is PolicyPhase.RETRY


@pytest.mark.parametrize("bad_facts", [
    None,
    {},
    {"registered": "yes", "allowed": True, "identity_valid": True},
    "model said so",
])
def test_model_input_cannot_construct_trusted_facts_or_request(bad_facts):
    if isinstance(bad_facts, dict):
        with pytest.raises((ValueError, TypeError)):
            VerifiedFacts(**bad_facts)
    with pytest.raises((ValueError, TypeError)):
        PolicyRequest(
            subject=SubjectRef(owner_id="owner"),
            action=ActionKind.MODEL_SEND,
            resource=ResourceRef(kind="model_invocation", id="invocation"),
            phase=PolicyPhase.SEND,
            facts=bad_facts,
        )


def test_decision_requires_the_verified_fact_type():
    with pytest.raises(ValueError):
        decide({"registered": True, "allowed": True, "identity_valid": True})


@pytest.mark.parametrize("action,phase", [
    (ActionKind.MODEL_SEND, PolicyPhase.CLAIM),
    (ActionKind.TASK_COMMIT, PolicyPhase.SEND),
    (ActionKind.NOTIFICATION_SEND, PolicyPhase.PREPARE),
    (ActionKind.TASK_QUERY, PolicyPhase.CLAIM),
    (ActionKind.TASK_CLAIM, PolicyPhase.COMMIT),
])
def test_decide_request_rejects_an_illegal_action_phase(action, phase):
    request = PolicyRequest(
        SubjectRef(owner_id="owner", thread_id="thread"), action,
        ResourceRef(kind="x", id="y", owner_id="owner"), phase, VerifiedFacts(True, True, True),
    )
    with pytest.raises(ValueError):
        decide_request(request)


def test_decide_request_rejects_a_resource_outside_the_subject():
    facts = VerifiedFacts(True, True, True)
    subject = SubjectRef(owner_id="owner", thread_id="thread")
    with pytest.raises(ValueError):
        decide_request(PolicyRequest(subject, ActionKind.MODEL_SEND,
            ResourceRef(kind="model_invocation", id="i", owner_id="other"), PolicyPhase.SEND, facts))
    with pytest.raises(ValueError):
        decide_request(PolicyRequest(subject, ActionKind.MODEL_SEND,
            ResourceRef(kind="model_invocation", id="i", owner_id="owner", thread_id="elsewhere"),
            PolicyPhase.SEND, facts))


def test_decide_request_allows_a_matching_request():
    request = PolicyRequest(
        SubjectRef(owner_id="owner", thread_id="thread"), ActionKind.MODEL_SEND,
        ResourceRef(kind="model_invocation", id="i", owner_id="owner", thread_id="thread"),
        PolicyPhase.SEND, VerifiedFacts(True, True, True),
    )
    assert decide_request(request).action is PolicyAction.ALLOW
