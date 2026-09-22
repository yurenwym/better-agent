"""V3 contract: exactly four top-level targets, everything else fails closed."""
import pytest

from app.learning_contract import (
    BEHAVIOR_MODEL_POLICY,
    BEHAVIOR_POLICY,
    BEHAVIOR_PROMPT,
    BEHAVIOR_REASONING_POLICY,
    BEHAVIOR_SUBTYPES,
    BEHAVIOR_TASK_POLICY,
    LearningContractError,
    TARGET_BEHAVIOR,
    TARGET_IGNORE,
    TARGET_MEMORY,
    TARGET_SKILL,
    TARGETS,
    asset_type_of,
    assert_target,
    is_learnable,
    is_target,
    legacy_assets_for_targets,
    normalize_target,
    target_of_asset,
)


def test_target_set_is_closed():
    assert TARGETS == ("MEMORY", "SKILL", "BEHAVIOR", "IGNORE")
    assert set(BEHAVIOR_SUBTYPES) == {"prompt", "task_policy", "policy", "reasoning_policy", "model_policy"}


@pytest.mark.parametrize("value", ["memory", "prompt", "task_policy", "evolution", "growth", "", None, 7, ["MEMORY"]])
def test_unknown_targets_fail_closed_to_ignore(value):
    assert normalize_target(value) == (TARGET_IGNORE, "")
    assert not is_target(value)
    assert not is_learnable(value)


def test_behavior_subtype_is_optional_at_decision_time():
    assert normalize_target(TARGET_BEHAVIOR, BEHAVIOR_PROMPT) == (TARGET_BEHAVIOR, "prompt")
    # JEV only picks the top-level target; the Behavior adapter refines the surface later.
    assert normalize_target(TARGET_BEHAVIOR) == (TARGET_BEHAVIOR, "")
    assert normalize_target(TARGET_BEHAVIOR, "unknown") == (TARGET_IGNORE, "")
    assert normalize_target(TARGET_MEMORY, "prompt") == (TARGET_IGNORE, "")


def test_assert_target_is_strict():
    assert assert_target(TARGET_MEMORY) == (TARGET_MEMORY, "")
    assert assert_target(TARGET_BEHAVIOR) == (TARGET_BEHAVIOR, "")
    assert is_learnable(TARGET_MEMORY) and is_learnable(TARGET_SKILL) and is_learnable(TARGET_BEHAVIOR)
    assert not is_learnable(TARGET_IGNORE)
    with pytest.raises(LearningContractError):
        assert_target(TARGET_BEHAVIOR, "nope")
    with pytest.raises(LearningContractError):
        assert_target(TARGET_MEMORY, "prompt")
    with pytest.raises(LearningContractError):
        target_of_asset("evolution")


@pytest.mark.parametrize(
    "asset,target,subtype",
    [
        ("memory", TARGET_MEMORY, ""),
        ("skill", TARGET_SKILL, ""),
        ("prompt", TARGET_BEHAVIOR, BEHAVIOR_PROMPT),
        ("task_policy", TARGET_BEHAVIOR, BEHAVIOR_TASK_POLICY),
        ("policy", TARGET_BEHAVIOR, BEHAVIOR_POLICY),
        ("reasoning_policy", TARGET_BEHAVIOR, BEHAVIOR_REASONING_POLICY),
        ("model_policy", TARGET_BEHAVIOR, BEHAVIOR_MODEL_POLICY),
    ],
)
def test_legacy_assets_map_onto_the_v3_contract(asset, target, subtype):
    assert target_of_asset(asset) == (target, subtype)
    assert asset_type_of(target, subtype) == asset


def test_legacy_target_list_covers_every_behavior_surface():
    assert legacy_assets_for_targets([TARGET_MEMORY, TARGET_SKILL]) == ["memory", "skill"]
    assert legacy_assets_for_targets([TARGET_BEHAVIOR]) == ["policy", "prompt", "task_policy"]
    assert legacy_assets_for_targets([TARGET_IGNORE]) == []
    with pytest.raises(LearningContractError):
        legacy_assets_for_targets(["EVOLUTION"])
    with pytest.raises(LearningContractError):
        legacy_assets_for_targets("MEMORY")
