"""Server-side boundaries that assemble verified PolicyRequests.

An adapter is the only place a boundary turns server state into a decision
input.  It never touches the database for a write and never decides anything:
it fixes the identity (subject), the target (resource), the lifecycle phase and
the already-checked facts, then hands the request to :func:`decide_request`.
Business services keep owning their state; the adapter only expresses what is
about to happen so the kernel can refuse it.

Resource ownership is an argument.  The adapter does not copy the subject onto
the resource.  Callers pass the owner and thread loaded from the resource row
(or, when the call has not been persisted yet, the trusted context that will
own it).  A missing resource is refused by the domain before this function is
asked to invent an owner.

The booleans passed in must come from server-side checks — never from model
output.  Model and tool sends, the adapters with production callers, take those
booleans as explicit parameters.  Adapters that are not yet called from
production still accept a closed set of fact names, but they do not default the
resource owner.
"""
from __future__ import annotations

from .policy_engine import (
    ActionKind,
    PolicyPhase,
    PolicyRequest,
    ResourceRef,
    SubjectRef,
    VerifiedFacts,
)


def _request(
    action: ActionKind,
    phase: PolicyPhase,
    *,
    owner_id: str,
    resource_kind: str,
    resource_id: str,
    resource_owner_id: str,
    facts: VerifiedFacts,
    thread_id: str | None = None,
    resource_thread_id: str | None = None,
    turn_id: str | None = None,
    task_id: str | None = None,
) -> PolicyRequest:
    return PolicyRequest(
        subject=SubjectRef(owner_id=owner_id, thread_id=thread_id, turn_id=turn_id, task_id=task_id),
        action=action,
        resource=ResourceRef(
            kind=resource_kind, id=resource_id,
            owner_id=resource_owner_id, thread_id=resource_thread_id,
        ),
        phase=phase,
        facts=facts,
    )


def _closed_facts(facts: dict) -> VerifiedFacts:
    """Fact bag for adapters that do not yet have a production caller.

    Unknown names are rejected.  Defaults remain only on this path; model and
    tool sends do not use it.
    """
    values = {
        "registered": True,
        "allowed": True,
        "identity_valid": True,
        "approval_required": False,
        "approval_granted": False,
        "cancelled": False,
        "source_valid": True,
        "budget_available": True,
        "execution_valid": True,
    }
    unknown = set(facts) - set(values)
    if unknown:
        raise TypeError("unknown policy facts: " + ", ".join(sorted(unknown)))
    values.update(facts)
    return VerifiedFacts(**values)


# --------------------------------------------------------------------------- #
# model send
# --------------------------------------------------------------------------- #

def model_send_request(
    *,
    owner_id: str,
    resource_owner_id: str,
    phase: PolicyPhase,
    invocation_id: str,
    thread_id: str | None = None,
    resource_thread_id: str | None = None,
    turn_id: str | None = None,
    registered: bool,
    allowed: bool,
    identity_valid: bool,
    source_valid: bool,
    budget_available: bool,
    execution_valid: bool,
    cancelled: bool,
    approval_required: bool = False,
    approval_granted: bool = False,
) -> PolicyRequest:
    """One attempt to reach the model for a frozen invocation."""
    return _request(
        ActionKind.MODEL_SEND, phase, owner_id=owner_id, resource_kind="model_invocation",
        resource_id=invocation_id, resource_owner_id=resource_owner_id,
        thread_id=thread_id, resource_thread_id=resource_thread_id, turn_id=turn_id,
        facts=VerifiedFacts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            approval_required=approval_required, approval_granted=approval_granted,
            cancelled=cancelled, source_valid=source_valid,
            budget_available=budget_available, execution_valid=execution_valid,
        ),
    )


# --------------------------------------------------------------------------- #
# tool execution
# --------------------------------------------------------------------------- #

def tool_execute_request(
    *,
    owner_id: str,
    resource_owner_id: str,
    tool_name: str,
    call_id: str | None = None,
    phase: PolicyPhase = PolicyPhase.SEND,
    thread_id: str | None = None,
    resource_thread_id: str | None = None,
    turn_id: str | None = None,
    registered: bool,
    allowed: bool,
    identity_valid: bool,
    source_valid: bool,
    budget_available: bool,
    execution_valid: bool,
    cancelled: bool,
    approval_required: bool = False,
    approval_granted: bool = False,
) -> PolicyRequest:
    """One dispatch of a tool call.  Resource ownership is supplied by the caller."""
    return _request(
        ActionKind.TOOL_EXECUTE, phase, owner_id=owner_id, resource_kind="tool",
        resource_id=call_id or tool_name, resource_owner_id=resource_owner_id,
        thread_id=thread_id, resource_thread_id=resource_thread_id, turn_id=turn_id,
        facts=VerifiedFacts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            approval_required=approval_required, approval_granted=approval_granted,
            cancelled=cancelled, source_valid=source_valid,
            budget_available=budget_available, execution_valid=execution_valid,
        ),
    )


def tool_resume_request(
    *,
    owner_id: str,
    resource_owner_id: str,
    tool_name: str,
    call_id: str,
    thread_id: str | None = None,
    resource_thread_id: str | None = None,
    turn_id: str | None = None,
    registered: bool,
    allowed: bool,
    identity_valid: bool,
    source_valid: bool,
    budget_available: bool,
    execution_valid: bool,
    cancelled: bool,
    approval_required: bool = False,
    approval_granted: bool = False,
) -> PolicyRequest:
    """Re-authorization of an approved call; the phase is the commit stage."""
    return tool_execute_request(
        owner_id=owner_id, resource_owner_id=resource_owner_id, tool_name=tool_name,
        call_id=call_id, phase=PolicyPhase.COMMIT, thread_id=thread_id,
        resource_thread_id=resource_thread_id, turn_id=turn_id,
        registered=registered, allowed=allowed, identity_valid=identity_valid,
        source_valid=source_valid, budget_available=budget_available,
        execution_valid=execution_valid, cancelled=cancelled,
        approval_required=approval_required, approval_granted=approval_granted,
    )


# --------------------------------------------------------------------------- #
# task execution rights — defined, not yet called from production (P02/P03)
# --------------------------------------------------------------------------- #

def _control_facts(
    *, registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
    approval_required: bool = False, approval_granted: bool = False,
) -> VerifiedFacts:
    return VerifiedFacts(
        registered=registered, allowed=allowed, identity_valid=identity_valid,
        approval_required=approval_required, approval_granted=approval_granted,
        cancelled=cancelled, source_valid=source_valid,
        budget_available=budget_available, execution_valid=execution_valid,
    )


def task_query_request(
    *, owner_id: str, resource_owner_id: str, task_id: str, task_kind: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
) -> PolicyRequest:
    return _request(
        ActionKind.TASK_QUERY, PolicyPhase.PREPARE, owner_id=owner_id,
        resource_kind=f"task.{task_kind}", resource_id=task_id,
        resource_owner_id=resource_owner_id, thread_id=thread_id,
        resource_thread_id=resource_thread_id, task_id=task_id,
        facts=_control_facts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            source_valid=source_valid, budget_available=budget_available,
            execution_valid=execution_valid, cancelled=cancelled,
        ),
    )


def _task_request(action: ActionKind, phase: PolicyPhase, **fields) -> PolicyRequest:
    facts = _control_facts(
        registered=fields.pop("registered"), allowed=fields.pop("allowed"),
        identity_valid=fields.pop("identity_valid"), source_valid=fields.pop("source_valid"),
        budget_available=fields.pop("budget_available"), execution_valid=fields.pop("execution_valid"),
        cancelled=fields.pop("cancelled"),
    )
    task_id = fields.pop("task_id")
    task_kind = fields.pop("task_kind")
    return _request(
        action, phase, resource_kind=f"task.{task_kind}", resource_id=task_id,
        task_id=task_id, facts=facts, **fields,
    )


def task_claim_request(
    *, owner_id: str, resource_owner_id: str, task_id: str, task_kind: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
) -> PolicyRequest:
    return _task_request(
        ActionKind.TASK_CLAIM, PolicyPhase.CLAIM, owner_id=owner_id,
        resource_owner_id=resource_owner_id, task_id=task_id, task_kind=task_kind,
        thread_id=thread_id, resource_thread_id=resource_thread_id,
        registered=registered, allowed=allowed, identity_valid=identity_valid,
        source_valid=source_valid, budget_available=budget_available,
        execution_valid=execution_valid, cancelled=cancelled,
    )


def task_heartbeat_request(
    *, owner_id: str, resource_owner_id: str, task_id: str, task_kind: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
) -> PolicyRequest:
    return _task_request(
        ActionKind.TASK_HEARTBEAT, PolicyPhase.CLAIM, owner_id=owner_id,
        resource_owner_id=resource_owner_id, task_id=task_id, task_kind=task_kind,
        thread_id=thread_id, resource_thread_id=resource_thread_id,
        registered=registered, allowed=allowed, identity_valid=identity_valid,
        source_valid=source_valid, budget_available=budget_available,
        execution_valid=execution_valid, cancelled=cancelled,
    )


def task_commit_request(
    *, owner_id: str, resource_owner_id: str, task_id: str, task_kind: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
) -> PolicyRequest:
    return _task_request(
        ActionKind.TASK_COMMIT, PolicyPhase.COMMIT, owner_id=owner_id,
        resource_owner_id=resource_owner_id, task_id=task_id, task_kind=task_kind,
        thread_id=thread_id, resource_thread_id=resource_thread_id,
        registered=registered, allowed=allowed, identity_valid=identity_valid,
        source_valid=source_valid, budget_available=budget_available,
        execution_valid=execution_valid, cancelled=cancelled,
    )


def task_cancel_request(
    *, owner_id: str, resource_owner_id: str, task_id: str, task_kind: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
) -> PolicyRequest:
    return _request(
        ActionKind.TASK_CANCEL, PolicyPhase.PREPARE, owner_id=owner_id, resource_kind=f"task.{task_kind}",
        resource_id=task_id, resource_owner_id=resource_owner_id, thread_id=thread_id,
        resource_thread_id=resource_thread_id, task_id=task_id,
        facts=_control_facts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            source_valid=source_valid, budget_available=budget_available,
            execution_valid=execution_valid, cancelled=cancelled,
        ),
    )


def decide_task_control(
    *, owner_id: str, caller_thread_id: str | None, resource_owner_id: str,
    resource_thread_id: str | None, task_id: str, task_kind: str, cancel: bool,
):
    """Query or cancel decision. Query is not claim or asset read.

    Budget and an already-terminal task do not withhold the action: stopping
    and reading stay available. The caller has already rejected a missing row
    and a thread the caller is bound to but does not match.
    """
    from .policy_engine import PolicyAction, decide_request

    subject_thread = caller_thread_id if caller_thread_id is not None else resource_thread_id
    builder = task_cancel_request if cancel else task_query_request
    try:
        decision = decide_request(builder(
            owner_id=owner_id, resource_owner_id=resource_owner_id, task_id=task_id,
            task_kind=task_kind, thread_id=subject_thread, resource_thread_id=resource_thread_id,
            registered=True, allowed=True, identity_valid=bool(owner_id and owner_id.strip()),
            source_valid=True, budget_available=True, execution_valid=True, cancelled=False,
        ))
    except ValueError:
        return None
    if decision.action is not PolicyAction.ALLOW:
        return None
    return decision


class AssetAuthorizationDenied(ValueError):
    """The asset write was refused. Callers must not insert a revision or adoption."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.policy_reason = reason
        super().__init__(detail or reason)


def _asset_facts(
    *, registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
    approval_required: bool, approval_granted: bool,
) -> VerifiedFacts:
    return VerifiedFacts(
        registered=registered, allowed=allowed, identity_valid=identity_valid,
        approval_required=approval_required, approval_granted=approval_granted,
        cancelled=cancelled, source_valid=source_valid,
        budget_available=budget_available, execution_valid=execution_valid,
    )


# --------------------------------------------------------------------------- #
# learning assets — called from asset reads and the promotion commit
# --------------------------------------------------------------------------- #

def asset_read_request(
    *, owner_id: str, resource_owner_id: str, asset_id: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
    approval_required: bool, approval_granted: bool,
) -> PolicyRequest:
    return _request(
        ActionKind.ASSET_READ, PolicyPhase.PREPARE, owner_id=owner_id, resource_kind="asset",
        resource_id=asset_id, resource_owner_id=resource_owner_id, thread_id=thread_id,
        resource_thread_id=resource_thread_id,
        facts=_asset_facts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            source_valid=source_valid, budget_available=budget_available,
            execution_valid=execution_valid, cancelled=cancelled,
            approval_required=approval_required, approval_granted=approval_granted,
        ),
    )


def asset_write_request(
    *, owner_id: str, resource_owner_id: str, asset_id: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
    approval_required: bool, approval_granted: bool,
) -> PolicyRequest:
    return _request(
        ActionKind.ASSET_WRITE, PolicyPhase.COMMIT, owner_id=owner_id, resource_kind="asset",
        resource_id=asset_id, resource_owner_id=resource_owner_id, thread_id=thread_id,
        resource_thread_id=resource_thread_id,
        facts=_asset_facts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            source_valid=source_valid, budget_available=budget_available,
            execution_valid=execution_valid, cancelled=cancelled,
            approval_required=approval_required, approval_granted=approval_granted,
        ),
    )


def asset_promote_request(
    *, owner_id: str, resource_owner_id: str, asset_id: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
    approval_required: bool, approval_granted: bool,
) -> PolicyRequest:
    return _request(
        ActionKind.ASSET_PROMOTE, PolicyPhase.COMMIT, owner_id=owner_id, resource_kind="asset",
        resource_id=asset_id, resource_owner_id=resource_owner_id, thread_id=thread_id,
        resource_thread_id=resource_thread_id,
        facts=_asset_facts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            source_valid=source_valid, budget_available=budget_available,
            execution_valid=execution_valid, cancelled=cancelled,
            approval_required=approval_required, approval_granted=approval_granted,
        ),
    )


def decide_asset(
    action: str, *, owner_id: str, resource_owner_id: str, asset_id: str,
    source_valid: bool, approval_required: bool, approval_granted: bool,
    thread_id: str | None = None, resource_thread_id: str | None = None,
):
    """Decide a read or adoption from an owner loaded with the asset row.

    A missing or mismatched owner returns None. The caller must not write.
    """
    from .policy_engine import decide_request

    builder = {"read": asset_read_request, "write": asset_write_request, "promote": asset_promote_request}[action]
    try:
        return decide_request(builder(
            owner_id=owner_id, resource_owner_id=resource_owner_id, asset_id=asset_id,
            thread_id=thread_id, resource_thread_id=resource_thread_id,
            registered=True, allowed=True,
            identity_valid=bool(owner_id and str(owner_id).strip()),
            source_valid=source_valid, budget_available=True, execution_valid=True,
            cancelled=False, approval_required=approval_required, approval_granted=approval_granted,
        ))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# notifications — called by NotificationService before a provider request
# --------------------------------------------------------------------------- #

def _notification_facts(
    *, registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
    approval_required: bool, approval_granted: bool,
) -> VerifiedFacts:
    return VerifiedFacts(
        registered=registered, allowed=allowed, identity_valid=identity_valid,
        approval_required=approval_required, approval_granted=approval_granted,
        cancelled=cancelled, source_valid=source_valid,
        budget_available=budget_available, execution_valid=execution_valid,
    )


def notification_send_request(
    *, owner_id: str, resource_owner_id: str, notification_id: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
    approval_required: bool, approval_granted: bool,
) -> PolicyRequest:
    return _request(
        ActionKind.NOTIFICATION_SEND, PolicyPhase.SEND, owner_id=owner_id,
        resource_kind="notification", resource_id=notification_id,
        resource_owner_id=resource_owner_id, thread_id=thread_id,
        resource_thread_id=resource_thread_id,
        facts=_notification_facts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            source_valid=source_valid, budget_available=budget_available,
            execution_valid=execution_valid, cancelled=cancelled,
            approval_required=approval_required, approval_granted=approval_granted,
        ),
    )


def notification_retry_request(
    *, owner_id: str, resource_owner_id: str, notification_id: str,
    thread_id: str | None = None, resource_thread_id: str | None = None,
    registered: bool, allowed: bool, identity_valid: bool, source_valid: bool,
    budget_available: bool, execution_valid: bool, cancelled: bool,
    approval_required: bool, approval_granted: bool,
) -> PolicyRequest:
    return _request(
        ActionKind.NOTIFICATION_RETRY, PolicyPhase.RETRY, owner_id=owner_id,
        resource_kind="notification", resource_id=notification_id,
        resource_owner_id=resource_owner_id, thread_id=thread_id,
        resource_thread_id=resource_thread_id,
        facts=_notification_facts(
            registered=registered, allowed=allowed, identity_valid=identity_valid,
            source_valid=source_valid, budget_available=budget_available,
            execution_valid=execution_valid, cancelled=cancelled,
            approval_required=approval_required, approval_granted=approval_granted,
        ),
    )


def decide_notification(
    action: str, *, owner_id: str, resource_owner_id: str, notification_id: str,
    source_valid: bool,
):
    """Decide a send or explicit retry. A mismatched owner returns None."""
    from .policy_engine import decide_request

    builder = notification_retry_request if action == "retry" else notification_send_request
    try:
        return decide_request(builder(
            owner_id=owner_id, resource_owner_id=resource_owner_id, notification_id=notification_id,
            registered=True, allowed=True,
            identity_valid=bool(owner_id and str(owner_id).strip()),
            source_valid=source_valid, budget_available=True, execution_valid=True,
            cancelled=False, approval_required=False, approval_granted=False,
        ))
    except ValueError:
        return None
