"""Pure authorization decisions over server-verified facts."""
from dataclasses import dataclass
from enum import StrEnum


class PolicyAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyReason(StrEnum):
    ALLOWED = "ALLOWED"
    UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
    CAPABILITY_NOT_ALLOWED = "CAPABILITY_NOT_ALLOWED"
    IDENTITY_INVALID = "IDENTITY_INVALID"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CANCELLED = "CANCELLED"
    SOURCE_INVALID = "SOURCE_INVALID"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    EXECUTION_RIGHT_LOST = "EXECUTION_RIGHT_LOST"


@dataclass(frozen=True)
class PolicyInput:
    registered: bool
    allowed: bool
    identity_valid: bool
    approval_required: bool = False
    approval_granted: bool = False
    cancelled: bool = False
    source_valid: bool = True
    budget_available: bool = True
    execution_valid: bool = True

    def __post_init__(self):
        if any(type(value) is not bool for value in vars(self).values()):
            raise ValueError("policy facts must be verified booleans")


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason: PolicyReason


def decide(facts: PolicyInput) -> PolicyDecision:
    for verified, reason in (
        (facts.registered, PolicyReason.UNKNOWN_CAPABILITY),
        (facts.allowed, PolicyReason.CAPABILITY_NOT_ALLOWED),
        (facts.identity_valid, PolicyReason.IDENTITY_INVALID),
        (not facts.cancelled, PolicyReason.CANCELLED),
        (facts.execution_valid, PolicyReason.EXECUTION_RIGHT_LOST),
        (facts.source_valid, PolicyReason.SOURCE_INVALID),
        (facts.budget_available, PolicyReason.BUDGET_EXHAUSTED),
    ):
        if not verified:
            return PolicyDecision(PolicyAction.DENY, reason)
    if facts.approval_required and not facts.approval_granted:
        return PolicyDecision(PolicyAction.REQUIRE_APPROVAL, PolicyReason.APPROVAL_REQUIRED)
    return PolicyDecision(PolicyAction.ALLOW, PolicyReason.ALLOWED)
