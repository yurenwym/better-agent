"""Pure authorization decisions over server-verified facts.

One kernel, one vocabulary.  :func:`decide` is the pure function; every entry
point (model send, tool execution, task control, asset and notification
actions) assembles a :class:`PolicyRequest` from facts it has *verified
server-side* and receives a :class:`PolicyDecision`.

Trust boundary: a ``VerifiedFacts`` value can only be built from verified
booleans.  There is deliberately no constructor that accepts a payload, a
mapping or model text, so untrusted input can never fabricate a trusted
identity, approval, source or budget fact.  Callers pass opaque
:class:`SubjectRef` / :class:`ResourceRef` references alongside facts they have
already checked against a durable store.
"""
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


class ActionKind(StrEnum):
    """The side-effecting action a request is about."""

    MODEL_SEND = "model.send"
    TOOL_EXECUTE = "tool.execute"
    TASK_QUERY = "task.query"
    TASK_CLAIM = "task.claim"
    TASK_HEARTBEAT = "task.heartbeat"
    TASK_COMMIT = "task.commit"
    TASK_CANCEL = "task.cancel"
    ASSET_READ = "asset.read"
    ASSET_WRITE = "asset.write"
    ASSET_PROMOTE = "asset.promote"
    NOTIFICATION_SEND = "notification.send"
    NOTIFICATION_RETRY = "notification.retry"


class PolicyPhase(StrEnum):
    """Where in a lifecycle the decision is being made."""

    PREPARE = "prepare"
    CLAIM = "claim"
    SEND = "send"
    RETRY = "retry"
    COMMIT = "commit"
    RECONCILE = "reconcile"


#: Which lifecycle phases each action is meaningful in.  A request that pairs
#: an action with a phase outside its set is malformed and refused before it can
#: reach an effect; the decision kernel stays free of this bookkeeping.
_ACTION_PHASES: dict[ActionKind, frozenset[PolicyPhase]] = {
    ActionKind.MODEL_SEND: frozenset({PolicyPhase.SEND, PolicyPhase.RETRY, PolicyPhase.COMMIT, PolicyPhase.RECONCILE}),
    ActionKind.TOOL_EXECUTE: frozenset({PolicyPhase.SEND, PolicyPhase.RETRY, PolicyPhase.COMMIT, PolicyPhase.RECONCILE}),
    ActionKind.TASK_QUERY: frozenset({PolicyPhase.PREPARE}),
    ActionKind.TASK_CLAIM: frozenset({PolicyPhase.PREPARE, PolicyPhase.CLAIM}),
    ActionKind.TASK_HEARTBEAT: frozenset({PolicyPhase.CLAIM, PolicyPhase.SEND}),
    ActionKind.TASK_COMMIT: frozenset({PolicyPhase.COMMIT}),
    ActionKind.TASK_CANCEL: frozenset({PolicyPhase.PREPARE, PolicyPhase.COMMIT, PolicyPhase.RECONCILE}),
    ActionKind.ASSET_READ: frozenset({PolicyPhase.PREPARE, PolicyPhase.SEND}),
    ActionKind.ASSET_WRITE: frozenset({PolicyPhase.PREPARE, PolicyPhase.COMMIT}),
    ActionKind.ASSET_PROMOTE: frozenset({PolicyPhase.PREPARE, PolicyPhase.COMMIT}),
    ActionKind.NOTIFICATION_SEND: frozenset({PolicyPhase.SEND, PolicyPhase.RETRY}),
    ActionKind.NOTIFICATION_RETRY: frozenset({PolicyPhase.RETRY, PolicyPhase.RECONCILE}),
}




def _require_text(value, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class SubjectRef:
    """Who is acting, as resolved from a trusted server-side identity."""

    owner_id: str
    thread_id: str | None = None
    turn_id: str | None = None
    task_id: str | None = None

    def __post_init__(self):
        _require_text(self.owner_id, "subject owner_id")
        for name in ("thread_id", "turn_id", "task_id"):
            if getattr(self, name) is not None:
                _require_text(getattr(self, name), name)


@dataclass(frozen=True)
class ResourceRef:
    """What the action targets; identity, not payload."""

    kind: str
    id: str
    owner_id: str | None = None
    thread_id: str | None = None

    def __post_init__(self):
        _require_text(self.kind, "resource kind")
        _require_text(self.id, "resource id")
        for name in ("owner_id", "thread_id"):
            if getattr(self, name) is not None:
                _require_text(getattr(self, name), name)


@dataclass(frozen=True)
class VerifiedFacts:
    """Facts a boundary has already checked against durable server state."""

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


#: Historical name for the boolean fact bundle.  Kept so existing boundaries
#: that already assemble verified booleans are not rewritten by the P01 change.
PolicyInput = VerifiedFacts


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason: PolicyReason


@dataclass(frozen=True)
class PolicyRequest:
    subject: SubjectRef
    action: ActionKind
    resource: ResourceRef
    phase: PolicyPhase
    facts: VerifiedFacts

    def __post_init__(self):
        if not isinstance(self.subject, SubjectRef):
            raise ValueError("subject must be a SubjectRef")
        if not isinstance(self.resource, ResourceRef):
            raise ValueError("resource must be a ResourceRef")
        if not isinstance(self.facts, VerifiedFacts):
            raise ValueError("a request carries only verified facts")
        object.__setattr__(self, "action", ActionKind(self.action))
        object.__setattr__(self, "phase", PolicyPhase(self.phase))


def validate_action_phase(action: ActionKind, phase: PolicyPhase) -> None:
    """Reject an action/phase pair the lifecycle cannot produce."""
    action, phase = ActionKind(action), PolicyPhase(phase)
    if phase not in _ACTION_PHASES[action]:
        raise ValueError(f"{action.value} is not valid in phase {phase.value}")


def validate_resource(resource: ResourceRef, subject: SubjectRef) -> None:
    """Reject a resource that does not belong to the requesting subject."""
    if not isinstance(resource, ResourceRef) or not isinstance(subject, SubjectRef):
        raise ValueError("resource and subject must be verified references")
    if resource.owner_id is not None and resource.owner_id != subject.owner_id:
        raise ValueError("resource owner does not match the subject")
    if resource.thread_id is not None:
        if subject.thread_id is None or resource.thread_id != subject.thread_id:
            raise ValueError("resource thread does not match the subject")


def decide(facts: VerifiedFacts) -> PolicyDecision:
    if not isinstance(facts, VerifiedFacts):
        raise ValueError("policy decisions require verified facts")
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


def decide_request(request: PolicyRequest) -> PolicyDecision:
    """Decide a full request.  Pure; the request must already be verified.

    Before touching the fact kernel the request shape is checked: the action
    must be valid in the phase it claims, and the resource must belong to the
    subject.  A malformed request is refused here, so no boundary can send or
    execute an action that was assembled against the wrong lifecycle stage or
    another owner's resource.
    """
    if not isinstance(request, PolicyRequest):
        raise ValueError("a verified PolicyRequest is required")
    validate_action_phase(request.action, request.phase)
    validate_resource(request.resource, request.subject)
    return decide(request.facts)
