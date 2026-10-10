import pytest

from app.policy_engine import PolicyAction, PolicyInput, PolicyReason, decide


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
