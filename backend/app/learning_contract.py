"""V3 learning contract: one closed target set for every learning outcome.

V3 collapses the old top-level targets (`prompt`, `task_policy`, `policy`) into
`BEHAVIOR` subtypes so an Experience has exactly one routing decision and one
pipeline. Anything outside this closed set fails closed to `IGNORE`; the harness
never learns by accident.
"""
from __future__ import annotations

from typing import Any, Literal

TARGET_MEMORY = "MEMORY"
TARGET_SKILL = "SKILL"
TARGET_BEHAVIOR = "BEHAVIOR"
TARGET_IGNORE = "IGNORE"

TARGETS: tuple[str, ...] = (TARGET_MEMORY, TARGET_SKILL, TARGET_BEHAVIOR, TARGET_IGNORE)

Target = Literal["MEMORY", "SKILL", "BEHAVIOR", "IGNORE"]
Risk = Literal["low", "medium", "high"]
RISKS: tuple[str, ...] = ("low", "medium", "high")

BEHAVIOR_PROMPT = "prompt"
BEHAVIOR_TASK_POLICY = "task_policy"
BEHAVIOR_POLICY = "policy"
BEHAVIOR_REASONING_POLICY = "reasoning_policy"
BEHAVIOR_MODEL_POLICY = "model_policy"

# Behavior subtypes stay closed as well: `policy` is the legacy umbrella the
# old `policy` target mapped to, the rest are the concrete runtime surfaces.
BEHAVIOR_SUBTYPES: tuple[str, ...] = (
    BEHAVIOR_PROMPT,
    BEHAVIOR_TASK_POLICY,
    BEHAVIOR_POLICY,
    BEHAVIOR_REASONING_POLICY,
    BEHAVIOR_MODEL_POLICY,
)

#: The subtypes this build can actually release, i.e. the ones with a runtime
#: surface the adapter can write and evaluate. The Learning LLM is offered only
#: these: proposing a subtype that has no release path wastes a cycle and, in
#: an acceptance run, guarantees a failure that says nothing about the model.
#: `BehaviorTargetAdapter` still refuses the others loudly — this is the prompt
#: side of the same contract, not a replacement for it.
BEHAVIOR_RELEASABLE_SUBTYPES: tuple[str, ...] = (BEHAVIOR_PROMPT, BEHAVIOR_MODEL_POLICY)

# Old `learning_policies.allowed_assets` values and old change_set
# `asset_type` values, mapped onto the V3 contract.
LEGACY_ASSET_TARGETS: dict[str, tuple[str, str]] = {
    "memory": (TARGET_MEMORY, ""),
    "skill": (TARGET_SKILL, ""),
    "prompt": (TARGET_BEHAVIOR, BEHAVIOR_PROMPT),
    "task_policy": (TARGET_BEHAVIOR, BEHAVIOR_TASK_POLICY),
    "policy": (TARGET_BEHAVIOR, BEHAVIOR_POLICY),
    "reasoning_policy": (TARGET_BEHAVIOR, BEHAVIOR_REASONING_POLICY),
    "model_policy": (TARGET_BEHAVIOR, BEHAVIOR_MODEL_POLICY),
}

# The inverse of LEGACY_ASSET_TARGETS, used when a V3 decision has to name the
# legacy asset it corresponds to.
TARGET_ASSET_TYPES: dict[tuple[str, str], str] = {value: key for key, value in LEGACY_ASSET_TARGETS.items()}


class LearningContractError(ValueError):
    """Raised when a value cannot be expressed in the V3 contract."""


def is_target(value: Any) -> bool:
    return isinstance(value, str) and value in TARGETS


def normalize_target(target: Any, subtype: Any = "") -> tuple[str, str]:
    """Return the canonical `(target, subtype)` pair.

    An empty subtype on `BEHAVIOR` is legitimate: JEV decides the top-level
    target only, and the Behavior adapter refines the concrete surface later.
    Unknown or malformed input fails closed to `IGNORE` instead of raising, so a
    broken producer can never be interpreted as a learning instruction.
    """
    if not is_target(target):
        return TARGET_IGNORE, ""
    if target == TARGET_BEHAVIOR:
        if subtype == "":
            return TARGET_BEHAVIOR, ""
        if isinstance(subtype, str) and subtype in BEHAVIOR_SUBTYPES:
            return TARGET_BEHAVIOR, subtype
        return TARGET_IGNORE, ""
    if subtype:
        return TARGET_IGNORE, ""
    return target, ""


def assert_target(target: Any, subtype: Any = "") -> tuple[str, str]:
    """Strict variant of :func:`normalize_target` for contract boundaries."""
    if not is_target(target):
        raise LearningContractError(f"unknown learning target: {target!r}")
    if target == TARGET_BEHAVIOR:
        if subtype == "":
            return TARGET_BEHAVIOR, ""
        if isinstance(subtype, str) and subtype in BEHAVIOR_SUBTYPES:
            return TARGET_BEHAVIOR, subtype
        raise LearningContractError(f"unknown behavior subtype: {subtype!r}")
    if subtype:
        raise LearningContractError(f"subtype {subtype!r} is not valid for target {target!r}")
    return target, ""


def target_of_asset(asset_type: Any) -> tuple[str, str]:
    """Map a legacy asset type onto `(target, subtype)`; unknown types raise."""
    if not isinstance(asset_type, str) or asset_type not in LEGACY_ASSET_TARGETS:
        raise LearningContractError(f"unknown legacy learning asset: {asset_type!r}")
    return LEGACY_ASSET_TARGETS[asset_type]


def asset_type_of(target: Any, subtype: Any = "") -> str:
    """Map a V3 target back onto the legacy asset type used by change sets."""
    canonical = assert_target(target, subtype)
    asset_type = TARGET_ASSET_TYPES.get(canonical)
    if asset_type is None:
        raise LearningContractError(f"target {canonical!r} has no asset type")
    return asset_type


def legacy_assets_for_targets(targets: Any) -> list[str]:
    """Translate a set of V3 targets into the legacy `allowed_assets` list."""
    if not isinstance(targets, (list, tuple, set, frozenset)):
        raise LearningContractError("learning targets must be a collection")
    assets: list[str] = []
    for target in targets:
        if target == TARGET_MEMORY:
            assets.append("memory")
        elif target == TARGET_SKILL:
            assets.append("skill")
        elif target == TARGET_BEHAVIOR:
            assets.extend(["prompt", "task_policy", "policy"])
        elif target == TARGET_IGNORE:
            continue
        else:
            raise LearningContractError(f"unknown learning target: {target!r}")
    return sorted(set(assets))


def is_learnable(target: Any) -> bool:
    return is_target(target) and target != TARGET_IGNORE
