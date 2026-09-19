from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol, TypeVar


class TokenCounter(Protocol):
    version: str

    def count_text(self, value: str) -> int: ...

    def count_payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int: ...


class Utf8UpperBoundTokenCounter:
    """A provider-independent upper bound for byte-fallback tokenizers.

    One token cannot encode less than one UTF-8 byte in the supported provider
    tokenizers. Counting canonical payload bytes therefore deliberately
    overestimates instead of risking a context overflow.
    """

    version = "utf8-upper-bound-v1"

    def count_text(self, value: str) -> int:
        return len(value.encode("utf-8"))

    def count_payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
        payload = {"messages": messages, "tools": tools or []}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self.count_text(encoded)


DEFAULT_TOKEN_COUNTER = Utf8UpperBoundTokenCounter()
DEFAULT_COUNTER_ID = "utf8-upper-bound"

COUNTER_MODE_ESTIMATE = "estimate"
COUNTER_MODE_VERIFIED = "verified"


@dataclass(frozen=True)
class CounterSelection:
    """The counter that will actually run for one profile, with its provenance.

    ``mode`` is ``verified`` only for an adapter whose output was checked
    against the provider's own accounting. Everything else is an ``estimate``
    and must not be presented as an exact token count.
    """

    counter: TokenCounter
    counter_id: str
    counter_version: str
    mode: str
    applicability: str
    evidence_version: str | None = None

    def public_view(self) -> dict[str, Any]:
        return {
            "counter_id": self.counter_id,
            "counter_version": self.counter_version,
            "mode": self.mode,
            "applicability": self.applicability,
            "evidence_version": self.evidence_version,
        }


# Registered adapters keyed by counter_id. Nothing is registered by default:
# an official tokenizer is only usable once its output has been verified
# against the provider's own usage accounting (T01/T12), and the conservative
# UTF-8 upper bound stays the safe fallback until then.
_COUNTER_ADAPTERS: dict[str, tuple[TokenCounter, str, str]] = {}


def register_token_counter(
    counter_id: str,
    counter: TokenCounter,
    *,
    mode: str,
    applicability: str,
) -> None:
    if not isinstance(counter_id, str) or not counter_id.strip():
        raise ValueError("counter_id must be a non-empty string")
    if mode not in {COUNTER_MODE_ESTIMATE, COUNTER_MODE_VERIFIED}:
        raise ValueError("counter mode must be estimate or verified")
    if not isinstance(applicability, str) or not applicability.strip():
        raise ValueError("counter applicability is required")
    _COUNTER_ADAPTERS[counter_id] = (counter, mode, applicability)


def counter_for_profile(profile: Any) -> CounterSelection:
    """Select the counter a profile's counter_id resolves to.

    Unknown or unregistered ids fall back to the conservative estimate with an
    explicit reason, so a profile can never silently run a different algorithm
    than the one recorded in its budget contract.
    """
    requested = str(getattr(profile, "counter_id", None) or DEFAULT_COUNTER_ID)
    evidence_version = getattr(profile, "counter_evidence_version", None)
    adapter = _COUNTER_ADAPTERS.get(requested)
    if adapter is not None:
        counter, mode, applicability = adapter
        if mode == COUNTER_MODE_VERIFIED and not (
            isinstance(evidence_version, str) and evidence_version.strip()
        ):
            return CounterSelection(
                DEFAULT_TOKEN_COUNTER, DEFAULT_COUNTER_ID, DEFAULT_TOKEN_COUNTER.version,
                COUNTER_MODE_ESTIMATE,
                f"counter {requested!r} is verified but the profile has no "
                "counter_evidence_version; using the conservative estimate",
                evidence_version,
            )
        return CounterSelection(counter, requested, counter.version, mode, applicability, evidence_version)
    return CounterSelection(
        DEFAULT_TOKEN_COUNTER, DEFAULT_COUNTER_ID, DEFAULT_TOKEN_COUNTER.version,
        COUNTER_MODE_ESTIMATE,
        f"counter {requested!r} has no verified adapter; using the conservative UTF-8 upper bound",
        evidence_version,
    )


@dataclass(frozen=True)
class ProtocolBudget:
    """Adapter-declared conservative wrapper overhead for A-tier counting.

    ``protocol_bound = b0 + bm*message_count + bt*tool_definition_count
    + bc*tool_call_count + br*tool_result_count``

    The coefficients are *not* fitted from data; they are the adapter's
    recorded non-negative conservative wrapper limits, with a unit and a
    provenance note. They never default to an arbitrary 4096, and an adapter
    without recorded evidence cannot claim A-tier counting.
    """

    b0: int = 0
    per_message: int = 0
    per_tool_definition: int = 0
    per_tool_call: int = 0
    per_tool_result: int = 0
    evidence: str = ""

    def __post_init__(self) -> None:
        for name in (
            "b0", "per_message", "per_tool_definition", "per_tool_call", "per_tool_result",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"protocol budget coefficient {name} must be a non-negative integer")

    @property
    def declared(self) -> bool:
        """True only when the adapter recorded where these limits come from."""
        return bool(self.evidence.strip())

    def overhead(
        self,
        *,
        message_count: int,
        tool_definition_count: int,
        tool_call_count: int,
        tool_result_count: int,
    ) -> int:
        return (
            self.b0
            + self.per_message * message_count
            + self.per_tool_definition * tool_definition_count
            + self.per_tool_call * tool_call_count
            + self.per_tool_result * tool_result_count
        )

    def public_view(self) -> dict[str, Any]:
        return {
            "b0": self.b0,
            "per_message": self.per_message,
            "per_tool_definition": self.per_tool_definition,
            "per_tool_call": self.per_tool_call,
            "per_tool_result": self.per_tool_result,
            "evidence": self.evidence,
            "declared": self.declared,
        }

# These keys are local packing hints. They are always removed before a request
# is handed to a provider.
CONTEXT_REQUIRED_KEY = "_context_required"
CONTEXT_PRIORITY_KEY = "_context_priority"
CONTEXT_GROUP_KEY = "_context_group"
_CONTEXT_KEYS = {CONTEXT_REQUIRED_KEY, CONTEXT_PRIORITY_KEY, CONTEXT_GROUP_KEY}


@dataclass(frozen=True)
class TokenBudget:
    context_window: int
    reserved_output: int
    safety_margin: int

    @property
    def input_limit(self) -> int:
        return max(self.context_window - self.reserved_output - self.safety_margin, 0)


class ContextOverflow(ValueError):
    pass


VALIDATION_TIER_A = "A"
VALIDATION_TIER_B = "B"
LEGACY_TIER = "legacy"


@dataclass(frozen=True)
class EffectiveInputBudget:
    """The single R1 budget contract.

    ``S = min(soft_context_limit, admitted_context_limit=A, context_window=C if verified)``
    ``H = S - O - safety_margin``

    ``A`` is never optional for a tier-A profile: there is deliberately no
    fallback to ``min(soft, C)``. Wrapper overhead is charged exactly once,
    through the A-tier counter's ``protocol_bound``, so a tier-A budget keeps
    ``safety_margin`` at zero instead of stacking a fixed guard on top.

    Legacy profiles (no ``validation_tier``) keep the historical
    ``C - O - proportional margin`` behaviour so existing frozen versions stay
    readable. They cannot reach the A-tier path without declaring evidence.
    """

    admitted_context_limit: int
    soft_context_limit: int
    context_window: int | None
    reserved_output: int
    safety_margin: int
    validation_tier: str
    counter_id: str
    counter_version: str
    evidence_version: str | None

    @property
    def total_limit(self) -> int:
        limits = [self.soft_context_limit, self.admitted_context_limit]
        if self.context_window is not None:
            limits.append(self.context_window)
        return min(limits)

    @property
    def input_limit(self) -> int:
        return max(self.total_limit - self.reserved_output - self.safety_margin, 0)

    def public_view(self) -> dict[str, Any]:
        return {
            "admitted_context_limit": self.admitted_context_limit,
            "soft_context_limit": self.soft_context_limit,
            "context_window": self.context_window,
            "max_output_tokens": self.reserved_output,
            "total_limit": self.total_limit,
            "input_limit": self.input_limit,
            "safety_margin": self.safety_margin,
            "validation_tier": self.validation_tier,
            "counter_id": self.counter_id,
            "counter_version": self.counter_version,
            "evidence_version": self.evidence_version,
        }


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


# Policy defaults for the two selection knobs. They are ratios and turn counts,
# never absolute byte constants: the selector must not bake in a fixed number of
# bytes. A profile that carries its own versioned values always wins.
DEFAULT_COMPACT_RATIO = 0.7
DEFAULT_HISTORY_MIN_TURNS = 5
MIN_TOOL_RESULT_BYTES = 256
# R2-02 recent-window ceiling. ``R = min(16384, floor(0.25 * H))`` while the
# static compaction target from R2-03 is not enabled. Expressed as a ratio of
# the model's own budget so it scales instead of being a bare byte constant.
DEFAULT_RECENT_WINDOW_BYTES = 16384
DEFAULT_RECENT_WINDOW_RATIO = 0.25
# R2-03 static early-archival policy. ``G = 30% H`` is the *low-water* line on
# the unarchived prefix: once the uncovered history crosses it a background pass
# starts, so the foreground normally never has to block. ``B = H - G`` is the
# headroom the foreground keeps, and ``D = 20% H`` is the reserve held back so
# the next growth cycle does not immediately re-trigger. ``T = B - D`` is the
# target the rebuilt count must come down to.
#
# These three ratios are *replay candidates* from the plan, not a claim about a
# measured p95. They are versioned policy with documented defaults so an
# operator can retune them from data without editing the selector.
DEFAULT_ARCHIVE_TRIGGER_RATIO = 0.30
DEFAULT_ARCHIVE_RESERVE_RATIO = 0.20
# Bytes of the non-history leading context (hard protection + mandatory
# summary) that R must leave room for. It defaults to 0 because the mandatory
# summary is retrieved *after* history is packed, so a non-zero default would
# reserve space for something not yet measured -- and over-reserving truncates
# the recent window early, which is the failure R2-02 explicitly forbids.
DEFAULT_ARCHIVE_PREFIX_RESERVE = 0
ARCHIVE_POLICY_VERSION = "static-archive-v1"


def history_min_turns(profile: Any) -> int:
    """Newest complete Turns the hot window must always carry."""
    value = getattr(profile, "history_min_turns", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_HISTORY_MIN_TURNS
    return value


def compact_ratio(profile: Any) -> float:
    """Fraction of ``H`` an archival pass must bring the rebuilt count down to."""
    value = getattr(profile, "compact_ratio", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= 1:
        return DEFAULT_COMPACT_RATIO
    return float(value)


def recent_window_ratio(profile: Any) -> float:
    """Fraction of ``H`` the soft-protected recent window may occupy."""
    value = getattr(profile, "recent_window_ratio", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= 1:
        return DEFAULT_RECENT_WINDOW_RATIO
    return float(value)


def recent_window_ceiling(profile: Any) -> int:
    """Absolute byte ceiling on the soft-protected recent window.

    A second term exists because the ratio alone lets a very large model spend
    an unbounded number of bytes on recency. Like the other policy knobs this is
    versioned profile policy with a documented default, not a literal in the
    selector.
    """
    value = getattr(profile, "recent_window_bytes", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_RECENT_WINDOW_BYTES
    return int(value)


def archive_trigger_ratio(profile: Any) -> float:
    """Fraction of ``H`` the unarchived prefix may reach before a pass starts."""
    value = getattr(profile, "archive_trigger_ratio", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) < 1:
        return DEFAULT_ARCHIVE_TRIGGER_RATIO
    return float(value)


def archive_reserve_ratio(profile: Any) -> float:
    """Fraction of ``H`` held back from ``B`` so the next cycle has room."""
    value = getattr(profile, "archive_reserve_ratio", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) < 1:
        return DEFAULT_ARCHIVE_RESERVE_RATIO
    return float(value)


def archive_prefix_reserve(profile: Any) -> int:
    """Declared bytes of leading non-history context that ``R`` must leave free."""
    value = getattr(profile, "archive_prefix_reserve", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_ARCHIVE_PREFIX_RESERVE
    return int(value)


@dataclass(frozen=True)
class StaticArchivePolicy:
    """The static early-archival line and target for one effective profile.

    All five numbers derive from ``H`` and two versioned ratios, so the policy
    scales with the model instead of drifting against a fixed byte constant.
    ``version`` is recorded on every archive job, because a job that cannot say
    which budget version produced it cannot be audited.
    """

    input_limit: int
    trigger: int
    headroom: int
    reserve: int
    target: int
    trigger_ratio: float
    reserve_ratio: float
    version: str
    # ``N`` travels with the policy so a background pass can never archive away
    # the newest complete turns the hot window is required to carry.
    min_turns: int

    def public_view(self) -> dict[str, Any]:
        return {
            "input_limit": self.input_limit,
            "trigger": self.trigger,
            "headroom": self.headroom,
            "reserve": self.reserve,
            "target": self.target,
            "trigger_ratio": self.trigger_ratio,
            "reserve_ratio": self.reserve_ratio,
            "version": self.version,
            "min_turns": self.min_turns,
        }


def static_archive_policy(
    profile: Any,
    *,
    trigger_ratio: float | None = None,
    reserve_ratio: float | None = None,
) -> StaticArchivePolicy:
    """``G``/``B``/``D``/``T`` for one profile.

    ``B = H - G`` and ``T = B - D``. ``T`` must stay positive, so the two ratios
    may not consume the whole budget; a profile that declares such a pair is a
    configuration error and is rejected rather than silently clamped.
    """
    resolved_trigger = archive_trigger_ratio(profile) if trigger_ratio is None else float(trigger_ratio)
    if not 0 < resolved_trigger < 1:
        raise ValueError("archive_trigger_ratio must be in (0, 1)")
    resolved_reserve = archive_reserve_ratio(profile) if reserve_ratio is None else float(reserve_ratio)
    if not 0 <= resolved_reserve < 1:
        raise ValueError("archive_reserve_ratio must be in [0, 1)")
    if resolved_trigger + resolved_reserve >= 1:
        raise ValueError(
            "archive_trigger_ratio + archive_reserve_ratio must be < 1 so the static target stays positive"
        )
    limit = effective_input_budget(profile).input_limit
    trigger = math.floor(resolved_trigger * limit)
    headroom = limit - trigger
    reserve = math.floor(resolved_reserve * limit)
    return StaticArchivePolicy(
        input_limit=limit,
        trigger=trigger,
        headroom=headroom,
        reserve=reserve,
        target=headroom - reserve,
        trigger_ratio=resolved_trigger,
        reserve_ratio=resolved_reserve,
        version=ARCHIVE_POLICY_VERSION,
        min_turns=history_min_turns(profile),
    )


def recent_window_budget(
    input_limit: int,
    *,
    ratio: float | None = None,
    ceiling: int | None = None,
    static_target: int | None = None,
    static_prefix: int = 0,
) -> int:
    """``R`` - the byte ceiling for the soft-protected recent window (R2-02).

    ``R = min(ceiling, floor(ratio * H))`` and, once a static archival target
    ``T`` and its already-compacted prefix ``P`` exist (R2-03), additionally
    ``max(0, T - P)``. Without R2-03 that third term is absent, exactly as the
    plan specifies.

    ``R`` exists so a long recent conversation cannot monopolise the budget and
    crowd out the summary. It is a ceiling on the *protected* window, not a
    licence to drop the newest turn: a turn that alone exceeds ``R`` is still
    carried, and the selector never skips a newer turn to backfill an older
    smaller one.
    """
    resolved_ratio = DEFAULT_RECENT_WINDOW_RATIO if ratio is None else float(ratio)
    if not 0 < resolved_ratio <= 1:
        raise ValueError("recent_window_ratio must be in (0, 1]")
    resolved_ceiling = DEFAULT_RECENT_WINDOW_BYTES if ceiling is None else int(ceiling)
    bounds = [max(resolved_ceiling, 0), math.floor(resolved_ratio * max(int(input_limit), 0))]
    if static_target is not None:
        bounds.append(max(0, int(static_target) - max(int(static_prefix), 0)))
    return max(min(bounds), 0)


def compact_target(budget: EffectiveInputBudget, *, ratio: float | None = None) -> int:
    """``ceil(compact_ratio * H)`` — the stop rule for one archival pass.

    Archiving must stop as soon as the rebuilt count reaches this target; the
    selector compresses only the smallest complete prefix that gets there and
    never compresses more "because it can".
    """
    resolved = DEFAULT_COMPACT_RATIO if ratio is None else float(ratio)
    if not 0 < resolved <= 1:
        raise ValueError("compact_ratio must be in (0, 1]")
    return math.ceil(resolved * budget.input_limit)


@dataclass(frozen=True)
class HotWindow:
    """The hot-window policy derived from one routed profile.

    ``input_limit`` is ``H``. ``packing_limit(message_count, tool_count)`` is
    ``H`` minus the adapter overhead measured for a request of that size, and is
    what the selector may actually fill, so the request it produces is one the
    wire gate accepts. An archival pass stops once the rebuilt count reaches
    ``compact_target``. No absolute byte constant is involved — every number
    traces back to the profile's versioned budget contract or to a measurement
    of the adapter that will send the request.
    """

    input_limit: int
    envelope_units: int
    per_message_units: int
    per_tool_units: int
    min_turns: int
    compact_target: int
    recent_window_bytes: int
    recent_window_ratio: float
    recent_window_ceiling: int
    tool_result_bytes: int
    validation_tier: str
    counter_version: str
    # R2-03 static early-archival policy, derived from the same profile.
    archive_trigger: int
    archive_headroom: int
    archive_reserve: int
    archive_target: int
    archive_trigger_ratio: float
    archive_reserve_ratio: float
    archive_prefix_reserve: int
    archive_policy_version: str
    # The frozen profile version every number above came from. Archive jobs
    # record it so a pass can be audited against the budget that produced it.
    profile_version_id: str | None = None
    # T08/T12: the resolved capacity provenance behind H. Recorded so runtime
    # evidence can say *why* the window is what it is instead of guessing.
    counter_id: str | None = None
    model_context_limit: int | None = None
    capacity_status: str | None = None
    capacity_source: str | None = None
    working_window_mode: str | None = None

    def packing_limit(self, message_count: int = 0, tool_count: int = 0) -> int:
        """The selector's budget for a request of this size."""
        overhead = self.envelope_for(message_count, tool_count)
        return max(self.input_limit - overhead, 0)

    def envelope_for(self, message_count: int = 0, tool_count: int = 0) -> int:
        """The adapter overhead reserved for a request of this size.

        This is the ``包装预算`` half of ``P``: it is *measured* from the adapter
        that will send the request, so it is exact rather than declared.
        """
        return (
            self.envelope_units
            + self.per_message_units * max(int(message_count), 0)
            + self.per_tool_units * max(int(tool_count), 0)
        )

    def recent_window_for(self, static_prefix: int = 0) -> int:
        """``R`` recomputed once ``P`` is known for the request being built.

        ``hot_window`` can only evaluate the third term with the envelope it has
        measured; a caller that also knows the mandatory summary and hard
        protection it is about to prepend passes their byte cost as
        ``static_prefix`` and gets the tighter, correct ceiling.
        """
        return recent_window_budget(
            self.input_limit,
            ratio=self.recent_window_ratio,
            ceiling=self.recent_window_ceiling,
            static_target=self.archive_target,
            static_prefix=max(int(static_prefix), 0),
        )

    def static_policy(self) -> StaticArchivePolicy:
        return StaticArchivePolicy(
            input_limit=self.input_limit,
            trigger=self.archive_trigger,
            headroom=self.archive_headroom,
            reserve=self.archive_reserve,
            target=self.archive_target,
            trigger_ratio=self.archive_trigger_ratio,
            reserve_ratio=self.archive_reserve_ratio,
            version=self.archive_policy_version,
            min_turns=self.min_turns,
        )

    def public_view(self) -> dict[str, Any]:
        return {
            "input_limit": self.input_limit,
            "envelope_units": self.envelope_units,
            "per_message_units": self.per_message_units,
            "per_tool_units": self.per_tool_units,
            "min_turns": self.min_turns,
            "compact_target": self.compact_target,
            "recent_window_bytes": self.recent_window_bytes,
            "recent_window_ratio": self.recent_window_ratio,
            "recent_window_ceiling": self.recent_window_ceiling,
            "tool_result_bytes": self.tool_result_bytes,
            "validation_tier": self.validation_tier,
            "counter_version": self.counter_version,
            "archive_trigger": self.archive_trigger,
            "archive_headroom": self.archive_headroom,
            "archive_reserve": self.archive_reserve,
            "archive_target": self.archive_target,
            "archive_trigger_ratio": self.archive_trigger_ratio,
            "archive_reserve_ratio": self.archive_reserve_ratio,
            "archive_prefix_reserve": self.archive_prefix_reserve,
            "archive_policy_version": self.archive_policy_version,
            "profile_version_id": self.profile_version_id,
            "counter_id": self.counter_id,
            "model_context_limit": self.model_context_limit,
            "capacity_status": self.capacity_status,
            "capacity_source": self.capacity_source,
            "working_window_mode": self.working_window_mode,
        }


def tool_result_budget(budget: EffectiveInputBudget) -> int:
    """Per-tool-result context ceiling, derived from the model's input budget.

    A single tool result may occupy at most an eighth of the request budget,
    floored at ``MIN_TOOL_RESULT_BYTES`` so a very small model can still carry a
    usable excerpt. Deriving it from ``H`` keeps it scaling with the model
    instead of drifting away as a fixed byte constant.
    """
    return max(budget.input_limit // 8, MIN_TOOL_RESULT_BYTES)


def hot_window(profile: Any, *, counter: TokenCounter | None = None) -> HotWindow:
    budget = effective_input_budget(profile, counter=counter)
    ratio = recent_window_ratio(profile)
    ceiling = recent_window_ceiling(profile)
    policy = static_archive_policy(profile)
    return HotWindow(
        input_limit=budget.input_limit,
        envelope_units=envelope_overhead(profile),
        per_message_units=per_message_overhead(profile),
        per_tool_units=per_tool_overhead(profile),
        min_turns=history_min_turns(profile),
        compact_target=compact_target(budget, ratio=compact_ratio(profile)),
        recent_window_bytes=recent_window_budget(
            budget.input_limit, ratio=ratio, ceiling=ceiling,
            static_target=policy.target, static_prefix=0,
        ),
        recent_window_ratio=ratio,
        recent_window_ceiling=ceiling,
        tool_result_bytes=tool_result_budget(budget),
        validation_tier=budget.validation_tier,
        counter_version=budget.counter_version,
        archive_trigger=policy.trigger,
        archive_headroom=policy.headroom,
        archive_reserve=policy.reserve,
        archive_target=policy.target,
        archive_trigger_ratio=policy.trigger_ratio,
        archive_reserve_ratio=policy.reserve_ratio,
        archive_prefix_reserve=archive_prefix_reserve(profile),
        archive_policy_version=policy.version,
        profile_version_id=getattr(profile, "registered_profile_version_id", None),
        counter_id=budget.counter_id,
        model_context_limit=getattr(profile, "model_context_limit", None),
        capacity_status=getattr(profile, "capacity_status", None),
        capacity_source=getattr(profile, "capacity_source", None),
        working_window_mode=getattr(profile, "working_window_mode", None),
    )


def effective_input_budget(
    profile: Any, *, counter: TokenCounter | None = None,
) -> EffectiveInputBudget:
    """The one budget entry point. No caller may re-implement the min().

    Raises :class:`ContextOverflow` when the profile cannot produce a valid
    budget. A tier-A profile without evidence is rejected rather than silently
    degraded to ``min(soft, C)``.

    ``counter`` lets a caller pin the exact algorithm it will also use for
    packing and the wire gate. When omitted, the profile's ``counter_id`` is
    resolved through :func:`counter_for_profile` so the recorded counter
    identity always matches the one that runs.
    """
    tier = getattr(profile, "validation_tier", None)
    if tier == VALIDATION_TIER_B:
        raise ContextOverflow("validation_tier 'B' is not implemented in this release")
    if tier not in (None, VALIDATION_TIER_A):
        raise ContextOverflow(f"unknown validation_tier {tier!r}")

    context_window = _positive_int_or_none(getattr(profile, "context_window", None))
    reserved_output = _positive_int_or_none(getattr(profile, "max_output_tokens", None))
    if reserved_output is None:
        raise ContextOverflow("model profile has no valid output reserve")

    selection = counter_for_profile(profile)
    if counter is None:
        counter_id = selection.counter_id
        counter_version = selection.counter_version
    else:
        counter_id = str(getattr(profile, "counter_id", None) or DEFAULT_COUNTER_ID)
        counter_version = str(getattr(counter, "version", DEFAULT_TOKEN_COUNTER.version))

    if tier is None:
        if context_window is None or reserved_output >= context_window:
            raise ContextOverflow("model profile has no valid context budget")
        available = context_window - reserved_output
        # Keep a proportional guard for normal models, but never let the
        # default margin consume the entire input budget of a small profile.
        margin = min(2048, max(1, available // 20))
        return EffectiveInputBudget(
            admitted_context_limit=context_window,
            soft_context_limit=context_window,
            context_window=context_window,
            reserved_output=reserved_output,
            safety_margin=margin,
            validation_tier=LEGACY_TIER,
            counter_id=counter_id,
            counter_version=counter_version,
            evidence_version=None,
        )

    admitted = _positive_int_or_none(getattr(profile, "admitted_context_limit", None))
    if admitted is None:
        raise ContextOverflow(
            "validation_tier A requires a positive admitted_context_limit (A); "
            "a repository default value is not capacity evidence"
        )
    evidence_version = getattr(profile, "counter_evidence_version", None)
    if not (isinstance(evidence_version, str) and evidence_version.strip()):
        raise ContextOverflow("validation_tier A requires counter_evidence_version")
    if reserved_output >= admitted:
        raise ContextOverflow("output reserve must be smaller than the admitted context limit")

    soft = _positive_int_or_none(getattr(profile, "soft_context_limit", None)) or admitted
    if getattr(profile, "working_window_mode", None) == "manual" and context_window is not None:
        soft = min(soft, context_window)
    # C only constrains the budget when its provenance is actually verified.
    # An unverified repository default must not silently cap an evidenced A.
    verified_window = context_window if bool(getattr(profile, "context_window_verified", False)) else None
    budget = EffectiveInputBudget(
        admitted_context_limit=admitted,
        soft_context_limit=soft,
        context_window=verified_window,
        reserved_output=reserved_output,
        safety_margin=0,
        validation_tier=VALIDATION_TIER_A,
        counter_id=counter_id,
        counter_version=counter_version,
        evidence_version=evidence_version,
    )
    if budget.input_limit <= 0:
        raise ContextOverflow("effective input budget is empty; check A, soft limit and output reserve")
    return budget


def request_budget(profile: Any, *, safety_margin: int | None = None) -> TokenBudget:
    """Legacy view over :func:`effective_input_budget`.

    Kept so frozen pre-R1 callers and tests keep their historical semantics.
    New code should consume ``effective_input_budget`` directly.
    """
    budget = effective_input_budget(profile)
    if safety_margin is None:
        margin = budget.safety_margin
    else:
        available = max(budget.total_limit - budget.reserved_output, 0)
        margin = max(0, min(int(safety_margin), max(available - 1, 0)))
    return TokenBudget(budget.total_limit, budget.reserved_output, margin)


T = TypeVar("T")


def pack_newest(
    items: Iterable[T],
    *,
    budget: int,
    render: Callable[[T], str],
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> tuple[list[T], list[T]]:
    """Pack newest atomic items, returning selected items chronologically."""

    ordered = list(items)
    selected_reversed: list[T] = []
    dropped: list[T] = []
    used = 0
    for item in reversed(ordered):
        cost = counter.count_text(render(item))
        if used + cost <= budget:
            selected_reversed.append(item)
            used += cost
        else:
            dropped.append(item)
    return list(reversed(selected_reversed)), list(reversed(dropped))


def pack_messages_newest(
    messages: Iterable[dict[str, Any]],
    *,
    budget: int,
    tools: list[dict[str, Any]] | None = None,
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> list[dict[str, Any]]:
    """Keep a hard-bounded request while preserving atomic message groups.

    System messages remain required by default for compatibility. Callers may
    mark data-only context (memory, episodes, plans, skills) as optional with
    ``_context_required=False`` and assign it a priority/group. Packing hints
    never leave this function.
    """
    ordered = [dict(message) for message in messages]
    if budget <= 0:
        if ordered or tools:
            raise ContextOverflow("model input budget is empty")
        return []
    latest_user_index = next(
        (index for index in range(len(ordered) - 1, -1, -1) if ordered[index].get("role") == "user"),
        None,
    )
    mandatory_indexes = set()
    for index, message in enumerate(ordered):
        required = message.get(CONTEXT_REQUIRED_KEY)
        if required is True or (required is None and message.get("role") == "system"):
            mandatory_indexes.add(index)
    if latest_user_index is not None:
        mandatory_indexes.add(latest_user_index)
    required_groups = {
        ordered[index].get(CONTEXT_GROUP_KEY) for index in mandatory_indexes
        if ordered[index].get(CONTEXT_GROUP_KEY)
    }
    mandatory_indexes.update(
        index for index, message in enumerate(ordered)
        if message.get(CONTEXT_GROUP_KEY) in required_groups
    )
    for index, message in enumerate(ordered):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        call_ids = {call.get("id") for call in message["tool_calls"] if isinstance(call, dict)}
        tool_group = {index}
        for offset in range(index + 1, len(ordered)):
            if ordered[offset].get("role") != "tool":
                break
            if ordered[offset].get("tool_call_id") in call_ids:
                tool_group.add(offset)
        if tool_group & mandatory_indexes:
            mandatory_indexes.update(tool_group)

    def clean(indexes: set[int]) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in ordered[index].items() if not key.startswith("_context_")}
            for index in sorted(indexes)
        ]

    selected_indexes = set(mandatory_indexes)
    selected = clean(selected_indexes)
    if counter.count_payload(selected, tools) > budget:
        raise ContextOverflow("required model messages exceed the input budget")

    groups: dict[str, set[int]] = {}
    consumed: set[int] = set(mandatory_indexes)
    for index, message in enumerate(ordered):
        if index in consumed:
            continue
        group_name = message.get(CONTEXT_GROUP_KEY)
        if group_name:
            indexes = {
                offset for offset, candidate in enumerate(ordered)
                if candidate.get(CONTEXT_GROUP_KEY) == group_name and offset not in mandatory_indexes
            }
            groups[f"context:{group_name}"] = indexes
            consumed.update(indexes)
            continue
        if message.get("role") == "user":
            indexes = {index}
            for offset in range(index + 1, len(ordered)):
                candidate = ordered[offset]
                if offset in mandatory_indexes or candidate.get(CONTEXT_GROUP_KEY):
                    break
                if candidate.get("role") in {"user", "system"}:
                    break
                indexes.add(offset)
            groups[f"turn:{index}"] = indexes
            consumed.update(indexes)
            continue
        if message.get("role") == "assistant" and message.get("tool_calls"):
            call_ids = {
                call.get("id") for call in message.get("tool_calls", [])
                if isinstance(call, dict) and call.get("id")
            }
            indexes = {index}
            for offset in range(index + 1, len(ordered)):
                candidate = ordered[offset]
                if candidate.get("role") != "tool":
                    break
                if candidate.get("tool_call_id") in call_ids:
                    indexes.add(offset)
            groups[f"tool:{index}"] = indexes
            consumed.update(indexes)
            continue
        if message.get("role") == "tool":
            consumed.add(index)
            continue
        groups[f"message:{index}"] = {index}
        consumed.add(index)

    def group_priority(indexes: set[int]) -> int:
        return max(int(ordered[index].get(CONTEXT_PRIORITY_KEY, 50)) for index in indexes)

    candidates = sorted(
        groups.items(),
        key=lambda item: (group_priority(item[1]), max(item[1])),
        reverse=True,
    )
    rejected_history_after: int | None = None
    for name, indexes in candidates:
        is_history = not name.startswith("context:")
        if is_history and rejected_history_after is not None and max(indexes) < rejected_history_after:
            continue
        proposed = selected_indexes | indexes
        packed = clean(proposed)
        if counter.count_payload(packed, tools) <= budget:
            selected_indexes = proposed
        elif is_history:
            # History is a chronological suffix. Do not skip a newer atomic
            # turn and then backfill an older, smaller turn around the gap.
            rejected_history_after = max(indexes)
    return clean(selected_indexes)


def assert_request_fits(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    profile: Any,
    *,
    counter: TokenCounter | None = None,
) -> int:
    """The last gate before transport. Returns the counted conservative units.

    This is the one place every call path funnels through, so the gate is
    tier-dispatched instead of letting each caller pick its own count:

    * tier A: ``U_A = byte_bound + protocol_bound``, including the adapter's
      declared wrapper overhead, compared against ``H``.
    * legacy: the historical byte-only payload bound, unchanged, so frozen
      pre-R1 profiles keep their exact previous behaviour.

    Local ``_context_*`` packing hints are stripped in both branches: the
    counter must see the effective provider messages, never packing metadata.
    """
    if counter is None:
        counter = counter_for_profile(profile).counter
    budget = effective_input_budget(profile, counter=counter)
    effective_messages = strip_packing_hints(messages)
    effective_tools = list(tools or [])

    if budget.validation_tier == VALIDATION_TIER_A:
        counted = count_request_units(effective_messages, effective_tools, profile, counter=counter)
        total = counted.total
        detail = (
            f" [byte_bound={counted.byte_bound} protocol_bound={counted.protocol_bound} "
            f"messages={counted.message_count} tool_definitions={counted.tool_definition_count} "
            f"tool_calls={counted.tool_call_count} tool_results={counted.tool_result_count} "
            f"counter={counted.counter_version} evidence={counted.evidence_version}]"
        )
    else:
        total = counter.count_payload(effective_messages, effective_tools)
        detail = ""

    if total > budget.input_limit:
        raise ContextOverflow(
            f"model context requires {total} conservative budget units "
            f"but the input budget is {budget.input_limit}{detail}"
        )
    return total


def assert_provider_payload_fits(
    payload: dict[str, Any],
    profile: Any,
    *,
    counter: TokenCounter | None = None,
) -> int:
    """Last gate of all: measure the exact body about to be serialised.

    ``assert_request_fits`` estimates the request from the canonical
    ``{"messages", "tools"}`` shape. Each provider adapter then re-serialises
    that request into its own wire format, and those shapes are not identical:
    the Anthropic adapter rewrites tool definitions into
    ``{"name", "description", "input_schema"}`` and hoists system turns into a
    top-level ``system`` string, the Gemini adapter builds ``contents`` plus
    ``functionDeclarations``. An adapter-level rewrite can therefore inflate a
    request that the estimate accepted.

    This gate removes that gap by counting the finished payload itself. It is
    strictly tighter than the estimate (it measures the real bytes rather than
    bytes plus declared wrapper overhead), so it can only ever reject a request
    the estimate wrongly admitted - never admit one it rejected.
    """
    if counter is None:
        counter = counter_for_profile(profile).counter
    total = wire_units(payload, counter=counter)
    budget = effective_input_budget(profile, counter=counter)
    if total > budget.input_limit:
        raise ContextOverflow(
            f"provider payload requires {total} conservative budget units "
            f"but the input budget is {budget.input_limit} "
            f"[wire_bound={total} keys={sorted(payload)} "
            f"counter={getattr(counter, 'version', 'unknown')}]")
    return total


def wire_units(payload: dict[str, Any], *, counter: TokenCounter = DEFAULT_TOKEN_COUNTER) -> int:
    """Count a finished provider payload exactly, using the wire gate's ruler.

    One definition of "the size of a request", shared by the gate and by the
    packing layer that has to leave room for it.
    """
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return counter.count_text(encoded)


# Request shapes the adapter envelope is measured over. Every variable-length
# scalar is measured at a length no request in this codebase exceeds (a
# 12-character float, an 8-digit token cap), so the maximum over these shapes is
# a reservation rather than a fitted value. ``{}`` covers the shape with no
# optional field set at all.
_ENVELOPE_SHAPES: tuple[dict[str, Any], ...] = (
    {},
    {"temperature": 0, "max_tokens": 8_192, "thinking": False},
    {"temperature": -0.123456789, "max_tokens": 99_999_999, "thinking": True,
     "response_format": {"type": "json_object"}},
)

# Probes used to measure how the adapter overhead *scales*. An
# envelope-preserving adapter adds a constant; a rewriting one adds per-message
# overhead (Gemini wraps every message in ``parts``), which a constant
# reservation cannot cover on a long conversation.
_ENVELOPE_PROBE_MESSAGE: dict[str, Any] = {"role": "user", "content": "x"}
_ENVELOPE_PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {"name": "probe", "description": "", "parameters": {"type": "object", "properties": {}}},
}


def _adapter_delta(
    profile: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    shape: dict[str, Any],
    counter: TokenCounter,
) -> int:
    """``wire_units - canonical_units`` for one concrete request."""
    from .model_gateway import ModelRequest, provider_payload

    request = ModelRequest(messages=messages, tools=tools, **shape)
    return (
        wire_units(provider_payload(profile, request), counter=counter)
        - counter.count_payload(messages, tools)
    )


def _overhead_slope(
    profile: Any,
    *,
    tools: list[dict[str, Any]],
    counter: TokenCounter,
    message_step: bool,
) -> int:
    """Measured marginal adapter overhead of one more message (or tool)."""
    shape = _ENVELOPE_SHAPES[-1]
    if message_step:
        one = _adapter_delta(profile, [_ENVELOPE_PROBE_MESSAGE], tools, shape, counter)
        two = _adapter_delta(profile, [_ENVELOPE_PROBE_MESSAGE] * 2, tools, shape, counter)
    else:
        one = _adapter_delta(profile, [], [_ENVELOPE_PROBE_TOOL], shape, counter)
        two = _adapter_delta(profile, [], [_ENVELOPE_PROBE_TOOL] * 2, shape, counter)
    return max(two - one, 0)


def envelope_overhead(
    profile: Any,
    *,
    message_count: int = 0,
    tool_count: int = 0,
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> int:
    """Counted units the adapter adds *around* the messages/tools it is given.

    ``pack_messages_newest`` counts only the canonical ``{"messages", "tools"}``
    payload. Every adapter then re-serialises that payload into its own wire
    format, which is *not* the same thing:

    * ``openai_compatible`` wraps it in an envelope - ``model``, ``stream``,
      ``stream_options``, ``temperature``, ``max_tokens``, ``thinking``,
      ``response_format`` - a measured constant (143 units for the 32k profile);
    * ``anthropic`` hoists ``system`` into a top-level string and reshapes tool
      definitions, which happens to come out *smaller* (a constant 24);
    * ``gemini`` rebuilds ``contents`` and wraps every message in ``parts``,
      which costs a measured **9 units per message** and therefore cannot be
      covered by any constant.

    A request packed to fill ``H`` on the canonical count was refused by the
    wire gate for exactly this difference. The reservation here is measured from
    the adapter, decomposed into a constant plus measured per-message and
    per-tool slopes, so it holds for a long conversation and not just a short
    one. Callers pass the size of the request they are about to pack, so the
    reservation is an upper bound for any subset the selector may choose.
    """
    base = max(
        _adapter_delta(profile, [], [], shape, counter) for shape in _ENVELOPE_SHAPES
    )
    return (
        max(base, 0)
        + _overhead_slope(profile, tools=[], counter=counter, message_step=True) * max(int(message_count), 0)
        + _overhead_slope(profile, tools=[], counter=counter, message_step=False) * max(int(tool_count), 0)
    )


def per_message_overhead(profile: Any, *, counter: TokenCounter = DEFAULT_TOKEN_COUNTER) -> int:
    """Measured marginal adapter overhead of one more message."""
    return _overhead_slope(profile, tools=[], counter=counter, message_step=True)


def per_tool_overhead(profile: Any, *, counter: TokenCounter = DEFAULT_TOKEN_COUNTER) -> int:
    """Measured marginal adapter overhead of one more tool definition."""
    return _overhead_slope(profile, tools=[], counter=counter, message_step=False)


def packing_limit(
    profile: Any,
    *,
    message_count: int = 0,
    tool_count: int = 0,
    counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> int:
    """``H`` minus the adapter overhead: the budget the selector may fill.

    The selector's ruler (canonical ``messages``/``tools``) and the gate's ruler
    (the finished wire body) are not the same ruler. Reserving the measured
    difference makes them agree, so a request the selector packed to the limit
    is a request the gate accepts.
    """
    budget = effective_input_budget(profile)
    return max(
        budget.input_limit
        - envelope_overhead(
            profile, message_count=message_count, tool_count=tool_count, counter=counter,
        ),
        0,
    )


def strip_packing_hints(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop local ``_context_*`` packing hints before counting or sending.

    The counter must see the effective provider messages, never the local
    packing metadata, and these keys must not reach a provider payload.
    """
    return [
        {key: value for key, value in message.items() if not key.startswith("_context_")}
        for message in messages
    ]


@dataclass(frozen=True)
class RequestCount:
    """A-tier count of the complete request that is about to be sent."""

    byte_bound: int
    protocol_bound: int
    mode: str
    counter_version: str
    evidence_version: str | None
    message_count: int
    tool_definition_count: int
    tool_call_count: int
    tool_result_count: int

    @property
    def total(self) -> int:
        """``U_A`` — a conservative budget in bytes, never a real token count."""
        return self.byte_bound + self.protocol_bound

    def public_view(self) -> dict[str, Any]:
        return {
            "byte_bound": self.byte_bound,
            "protocol_bound": self.protocol_bound,
            "total": self.total,
            "mode": self.mode,
            "counter_version": self.counter_version,
            "evidence_version": self.evidence_version,
            "message_count": self.message_count,
            "tool_definition_count": self.tool_definition_count,
            "tool_call_count": self.tool_call_count,
            "tool_result_count": self.tool_result_count,
        }


def count_request_units(
    messages: Iterable[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    profile: Any,
    *,
    counter: TokenCounter | None = None,
) -> RequestCount:
    """Count the complete request once, in the profile's declared mode.

    A-tier: ``U_A = byte_bound + protocol_bound``. B-tier calibration is not
    implemented in this release and is refused rather than faked with
    ``alpha=1, beta=0``. When ``counter`` is omitted it is resolved from the
    profile so the recorded counter identity matches the algorithm that runs.
    """
    if counter is None:
        counter = counter_for_profile(profile).counter
    effective = strip_packing_hints(messages)
    tool_list = list(tools or [])
    tool_calls = 0
    tool_results = 0
    for message in effective:
        if message.get("role") == "tool":
            tool_results += 1
            continue
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            tool_calls += sum(1 for call in raw_calls if isinstance(call, dict))
    byte_bound = counter.count_payload(effective, tool_list)
    protocol_budget = getattr(profile, "protocol_budget", None)
    if protocol_budget is not None and not isinstance(protocol_budget, ProtocolBudget):
        raise ValueError("profile protocol_budget must be a ProtocolBudget")
    if protocol_budget is None or not protocol_budget.declared:
        raise ContextOverflow(
            "A-tier counting requires a declared protocol budget with recorded "
            "provenance; the adapter has no evidence for its wrapper overhead"
        )
    protocol_bound = protocol_budget.overhead(
        message_count=len(effective),
        tool_definition_count=len(tool_list),
        tool_call_count=tool_calls,
        tool_result_count=tool_results,
    )
    return RequestCount(
        byte_bound=byte_bound,
        protocol_bound=protocol_bound,
        mode="A",
        counter_version=getattr(counter, "version", DEFAULT_TOKEN_COUNTER.version),
        evidence_version=getattr(profile, "counter_evidence_version", None),
        message_count=len(effective),
        tool_definition_count=len(tool_list),
        tool_call_count=tool_calls,
        tool_result_count=tool_results,
    )
