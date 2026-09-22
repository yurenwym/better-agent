"""The V3 learning switch: off by default, loud when half-wired.

`BETTER_AGENT_LEARNING_V3` decides whether the one Learning Pipeline is attached
at all. A pipeline that is attached but missing a credential, or missing a
generator, would silently learn nothing — so wiring fails loudly instead of
degrading, and this suite pins that behaviour.
"""
import pytest

from app.startup import LEARNING_V3_MODES, _wire_learning_pipeline, build_runtime

WIRING_ENV = ("BETTER_AGENT_LEARNING_V3", "TYPESAFE_API_KEY", "TYPESAFE_MODEL",
              "BETTER_AGENT_LEARNING_REPLAY_FILE", "BETTER_AGENT_LEARNING_CANARY_BUDGET_MICROUSD")


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH",
                 *WIRING_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BETTER_AGENT_TEST_ALLOW_SQLITE", "1")


class FakeGateway:
    """The wiring path only needs something truthy to hand to the agent and judge."""

    def identity(self):
        return "stub:test"


@pytest.fixture
def runtime(tmp_path):
    """Built with the switch off, so the wiring can be exercised in isolation."""
    return build_runtime(tmp_path / "data")


def test_the_switch_is_off_by_default(runtime):
    assert runtime.learning.pipeline is None


def test_an_unset_switch_leaves_the_legacy_path_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "off")
    assert build_runtime(tmp_path / "data").learning.pipeline is None


def test_a_credential_alone_does_not_arm_the_pipeline(tmp_path, monkeypatch):
    """`build_runtime` calls the wiring itself; a stray key must not attach it."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    assert build_runtime(tmp_path / "data").learning.pipeline is None


@pytest.mark.parametrize("mode", sorted(LEARNING_V3_MODES))
def test_a_named_mode_attaches_the_pipeline(runtime, monkeypatch, mode):
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", mode)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-latest")

    _wire_learning_pipeline(runtime, FakeGateway())

    assert runtime.learning.pipeline is not None
    assert runtime.learning.pipeline.mode == LEARNING_V3_MODES[mode]
    from app.learning_replay import RuntimeLearningReplay
    assert isinstance(runtime.learning.pipeline.replay, RuntimeLearningReplay)


def test_an_unknown_mode_is_refused(runtime, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "CANARY")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    with pytest.raises(ValueError, match="invalid BETTER_AGENT_LEARNING_V3"):
        _wire_learning_pipeline(runtime, FakeGateway())


def test_explicit_canary_budget_is_wired(runtime, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "SHADOW")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("BETTER_AGENT_LEARNING_CANARY_BUDGET_MICROUSD", "12345")
    _wire_learning_pipeline(runtime, FakeGateway())
    policy = runtime.learning.pipeline.gate.policy
    assert policy.canary_budget_microusd == 12345
    assert (policy.canary_target_role, policy.canary_target_purpose) == ("researcher", "write_research_section")


@pytest.mark.parametrize("value", ["0", "-1", "oops"])
def test_invalid_canary_budget_is_refused(runtime, monkeypatch, value):
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "SHADOW")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("BETTER_AGENT_LEARNING_CANARY_BUDGET_MICROUSD", value)
    with pytest.raises(ValueError, match="positive integer"):
        _wire_learning_pipeline(runtime, FakeGateway())


def test_a_missing_credential_is_refused_not_degraded(runtime, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "SHADOW")
    with pytest.raises(RuntimeError, match="requires TYPESAFE_API_KEY"):
        _wire_learning_pipeline(runtime, FakeGateway())


def test_a_missing_gateway_is_refused(runtime, monkeypatch):
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "SHADOW")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    with pytest.raises(RuntimeError, match="requires a model gateway"):
        _wire_learning_pipeline(runtime, None)


def test_the_attached_pipeline_owns_the_learning_route(runtime, monkeypatch):
    """A wired pipeline is what `execute` routes to; nothing else is consulted."""
    monkeypatch.setenv("BETTER_AGENT_LEARNING_V3", "SHADOW")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    _wire_learning_pipeline(runtime, FakeGateway())
    assert runtime.learning.pipeline.learning is runtime.learning
    assert runtime.learning.pipeline.mode == LEARNING_V3_MODES["SHADOW"]
