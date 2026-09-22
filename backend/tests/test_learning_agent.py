"""Learning LLM: typed drafts, evidence binding, and no authority at all."""
import json
from types import SimpleNamespace

import pytest

from app.learning_agent import (
    BehaviorDraft,
    DraftError,
    LearningAgent,
    MemoryDraft,
    SkillDraft,
    _extract_json,
    build_messages,
    evidence_digest,
    parse_draft,
)
from app.learning_contract import TARGET_BEHAVIOR, TARGET_MEMORY, TARGET_SKILL
from app.learning_decision import LearningDecision

EXPERIENCE = {"id": "experience_1", "task_type": "incident_diagnosis", "outcome": "success"}
EVIDENCE_IDS = ("experience_1",)
SOURCE_REFS = ("thread_message_1",)


def decision(target=TARGET_MEMORY, *, learn=True, subtype=""):
    return LearningDecision(learn=learn, target=target if learn else "IGNORE", subtype=subtype,
                            confidence=0.9, importance=0.7, risk="low", reason_codes=("explicit_user_constraint",))


def memory_payload(**over):
    payload = {
        "target": "MEMORY",
        "problem": "用户显式要求生产库不能自动重启",
        "root_cause": "该约束此前没有被持久化",
        "generalizable_lesson": "生产库重启必须人工批准",
        "proposed_change": {"operation": "ADD", "kind": "constraint", "scope_type": "user", "scope_id": "",
                            "content": "生产数据库不能由 Agent 自动重启，必须人工批准。"},
        "expected_effect": {"future_restarts": "require approval"},
        "risks": ["约束过宽会阻碍正常运维"],
        "evidence_refs": ["thread_message_1"],
        "experience_ids": ["experience_1"],
    }
    payload.update(over)
    return payload


def skill_payload(**over):
    payload = {
        "target": "SKILL",
        "problem": "数据库超时反复出现，每次都重新摸索",
        "root_cause": "缺少固定的排查顺序",
        "generalizable_lesson": "先查连接池，再查慢 SQL，再查最近发布，最后验证连接泄漏",
        "proposed_change": {"name": "database-timeout-diagnosis", "title": "数据库超时诊断",
                            "description": "按固定顺序定位数据库超时。", "kind": "instruction_only",
                            "content": "# 数据库超时诊断\n1. 检查连接池\n2. 检查慢 SQL\n输出：定位结论与证据。\n退出：任务与数据库超时无关时停止。",
                            "requested_tools": [], "connectors": [], "phases": ["conversation"]},
        "expected_effect": {"tool_calls": "fewer"},
        "risks": [],
        "evidence_refs": ["thread_message_1"],
        "experience_ids": ["experience_1"],
    }
    payload.update(over)
    return payload


def behavior_payload(**over):
    payload = {
        "target": "BEHAVIOR",
        "problem": "只有一条证据就确认根因",
        "root_cause": "缺少证据充分性检查",
        "generalizable_lesson": "确认根因前要求充分证据",
        "proposed_change": {"subtype": "prompt", "surface": "researcher system prompt",
                            "change": {"append": "确认根因前必须给出至少两条独立证据。"}, "rationale": "降低误判"},
        "expected_effect": {"false_root_cause": "down"},
        "risks": ["可能增加交互轮次"],
        "evidence_refs": ["thread_message_1"],
        "experience_ids": ["experience_1"],
    }
    payload.update(over)
    return payload


class StubGateway:
    profile = SimpleNamespace(provider_name="deepseek", model="deepseek-flash")

    def __init__(self, reply):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        self.requests = []

    async def complete(self, request, context=None, **kwargs):
        self.requests.append({"request": request, "context": context})
        return SimpleNamespace(message=self.reply)


def parse(payload, *, decision_value=None, **kwargs):
    return parse_draft(decision_value or decision(), payload, experience_ids=EVIDENCE_IDS,
                       source_refs=SOURCE_REFS, evidence=EXPERIENCE, **kwargs)


@pytest.mark.parametrize("payload,expected_type", [
    (memory_payload(), MemoryDraft),
    (skill_payload(), SkillDraft),
    (behavior_payload(), BehaviorDraft),
])
def test_each_target_parses_into_its_own_type(payload, expected_type):
    draft = parse(payload, decision_value=decision(payload["target"]))
    assert isinstance(draft, expected_type)
    assert draft.target == payload["target"]
    assert draft.problem and draft.root_cause and draft.generalizable_lesson


def test_memory_draft_normalises_its_change():
    draft = parse(memory_payload(), decision_value=decision(TARGET_MEMORY))
    assert isinstance(draft, MemoryDraft)
    assert draft.operation == "ADD" and draft.kind == "constraint" and draft.scope == ("user", "")
    assert "人工批准" in draft.content


def test_skill_draft_carries_no_tool_authority():
    draft = parse(skill_payload(), decision_value=decision(TARGET_SKILL))
    assert isinstance(draft, SkillDraft)
    assert draft.requested_tools == ()
    assert draft.proposed_change["kind"] == "instruction_only"


def test_behavior_draft_keeps_its_subtype():
    draft = parse(behavior_payload(), decision_value=decision(TARGET_BEHAVIOR))
    assert isinstance(draft, BehaviorDraft)
    assert draft.subtype == "prompt"
    assert draft.proposed_change["surface"] == "researcher system prompt"


def test_a_draft_cannot_change_the_decided_target():
    with pytest.raises(DraftError, match="changed the decided target"):
        parse(skill_payload(), decision_value=decision(TARGET_MEMORY))


def test_an_ignore_decision_has_no_candidate():
    with pytest.raises(DraftError, match="IGNORE"):
        parse(memory_payload(), decision_value=decision(learn=False))


@pytest.mark.parametrize("payload,message", [
    (memory_payload(proposed_change={"operation": "ADD", "kind": "opinion", "scope_type": "user", "scope_id": "", "content": "x"}), "memory kind"),
    (memory_payload(proposed_change={"operation": "ADD", "kind": "fact", "scope_type": "project", "scope_id": "", "content": "x"}), "scope_id"),
    (memory_payload(proposed_change={"operation": "ADD", "kind": "fact", "scope_type": "user", "scope_id": "p", "content": "x"}), "cannot carry scope_id"),
    (memory_payload(proposed_change={"operation": "MERGE", "kind": "fact", "scope_type": "user", "scope_id": "", "content": "x"}), "memory operation"),
    (memory_payload(proposed_change={"operation": "ADD", "kind": "fact", "scope_type": "user", "scope_id": "", "content": "  "}), "content is required"),
    (skill_payload(proposed_change={"name": "Bad Name", "kind": "instruction_only", "content": "输出：x\n退出：y"}), "lowercase slug"),
    (skill_payload(proposed_change={"name": "ok-name", "kind": "instruction_only", "content": "no markers"}), "must declare"),
    (skill_payload(proposed_change={"name": "ok-name", "kind": "tool_bound", "content": "输出：x\n退出：y", "requested_tools": []}), "skill kind"),
    (skill_payload(proposed_change={"name": "ok-name", "kind": "instruction_only", "content": "输出：x\n退出：y",
                                    "requested_tools": ["shell"]}), "may not request new tool"),
    (behavior_payload(proposed_change={"subtype": "unknown", "surface": "x", "change": {"a": 1}}), "behavior subtype"),
    (behavior_payload(proposed_change={"subtype": "prompt", "change": {"a": 1}}), "runtime surface"),
    (behavior_payload(proposed_change={"subtype": "prompt", "surface": "x", "change": {}}), "non-empty object"),
])
def test_malformed_proposed_changes_are_refused(payload, message):
    with pytest.raises(DraftError, match=message):
        parse(payload, decision_value=decision(payload["target"]))


@pytest.mark.parametrize("payload,message", [
    (memory_payload(problem=""), "must not be empty"),
    (memory_payload(problem=7), "must be a string"),
    ({k: v for k, v in memory_payload().items() if k != "generalizable_lesson"}, "missing field"),
    (memory_payload(expected_effect=[1, 2]), "must be an object"),
    (memory_payload(risks="oops"), "must be a list"),
])
def test_malformed_envelope_fields_are_refused(payload, message):
    with pytest.raises(DraftError, match=message):
        parse(payload, decision_value=decision(payload.get("target", TARGET_MEMORY)))


def test_a_candidate_without_evidence_is_refused():
    with pytest.raises(DraftError, match="without evidence"):
        parse_draft(decision(), memory_payload(), experience_ids=(), source_refs=(), evidence=EXPERIENCE)


def test_a_thread_message_source_is_enough_evidence():
    """A message has no Experience row, but it is still the evidence held."""
    payload = memory_payload(experience_ids=[])
    draft = parse_draft(decision(), payload, experience_ids=(), source_refs=SOURCE_REFS,
                        evidence={"id": "thread_message_1", "text": "以后生产数据库不能自动重启"})
    assert draft.target == TARGET_MEMORY
    assert draft.experience_ids == ()
    assert draft.evidence_refs == SOURCE_REFS
    assert draft.evidence_digest == evidence_digest(experience_ids=(), source_refs=SOURCE_REFS,
                                                    evidence={"id": "thread_message_1",
                                                              "text": "以后生产数据库不能自动重启"})


def test_the_memory_prompt_states_the_legal_scopes():
    messages = build_messages(decision(TARGET_MEMORY), experience=EXPERIENCE, evidence_ids=EVIDENCE_IDS,
                              source_refs=SOURCE_REFS)
    assert "safety_constraints.memory_scopes" in messages[0]["content"]
    skill_messages = build_messages(decision(TARGET_SKILL), experience=EXPERIENCE, evidence_ids=EVIDENCE_IDS,
                                    source_refs=SOURCE_REFS)
    assert "memory_scopes" not in skill_messages[0]["content"]


def test_a_candidate_cannot_invent_evidence():
    with pytest.raises(DraftError, match="claims experience it was not given"):
        parse(memory_payload(experience_ids=["experience_999"]), decision_value=decision(TARGET_MEMORY))
    with pytest.raises(DraftError, match="cites evidence it was not given"):
        parse(memory_payload(evidence_refs=["thread_message_999"]), decision_value=decision(TARGET_MEMORY))


def test_either_evidence_field_may_cite_either_offered_list():
    """The two lists differ for the harness, not for the model."""
    draft = parse(memory_payload(experience_ids=["thread_message_1"]), decision_value=decision(TARGET_MEMORY))
    assert draft.experience_ids == EVIDENCE_IDS  # the harness's own binding, not the model's
    other = parse(memory_payload(evidence_refs=["experience_1"]), decision_value=decision(TARGET_MEMORY))
    assert other.evidence_refs == ("experience_1",)


def test_evidence_digest_is_computed_by_the_harness():
    draft = parse(memory_payload(), decision_value=decision(TARGET_MEMORY))
    expected = evidence_digest(experience_ids=EVIDENCE_IDS, source_refs=SOURCE_REFS, evidence=EXPERIENCE)
    assert draft.evidence_digest == expected
    # A different Experience must produce a different binding.
    other = parse_draft(decision(), memory_payload(), experience_ids=EVIDENCE_IDS, source_refs=SOURCE_REFS,
                        evidence={"id": "experience_1", "outcome": "failure"})
    assert other.evidence_digest != draft.evidence_digest


def test_agent_has_no_tools():
    agent = LearningAgent(StubGateway(memory_payload()))
    assert agent.tools == []


def test_agent_generates_a_draft_from_a_model_reply():
    gateway = StubGateway(memory_payload())
    agent = LearningAgent(gateway)
    draft = agent.generate(decision=decision(TARGET_MEMORY), experience=EXPERIENCE, trace=[{"tool": "probe"}],
                           asset_state={"memory_entries": 2}, evidence_ids=EVIDENCE_IDS, source_refs=SOURCE_REFS)
    assert isinstance(draft, MemoryDraft)
    assert draft.model_identity == "deepseek:deepseek-flash"
    sent = gateway.requests[0]["request"]
    assert sent.tools == [] and sent.temperature == 0 and sent.role == "learning_generator"
    assert gateway.requests[0]["context"] is None


def test_agent_refuses_a_reply_that_is_not_json():
    agent = LearningAgent(StubGateway("I think we should improve the prompt."))
    with pytest.raises(DraftError, match="no JSON object"):
        agent.generate(decision=decision(TARGET_MEMORY), experience=EXPERIENCE,
                       evidence_ids=EVIDENCE_IDS, source_refs=SOURCE_REFS)


def test_agent_refuses_when_no_model_is_configured():
    agent = LearningAgent(None)
    with pytest.raises(DraftError, match="not configured"):
        agent.generate(decision=decision(TARGET_MEMORY), experience=EXPERIENCE,
                       evidence_ids=EVIDENCE_IDS, source_refs=SOURCE_REFS)


def test_agent_refuses_an_ignore_decision():
    agent = LearningAgent(StubGateway(memory_payload()))
    with pytest.raises(DraftError, match="IGNORE"):
        agent.generate(decision=decision(learn=False), experience=EXPERIENCE,
                       evidence_ids=EVIDENCE_IDS, source_refs=SOURCE_REFS)


def test_fenced_json_is_accepted():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('prose {"a": 1} trailing') == {"a": 1}
    with pytest.raises(DraftError):
        _extract_json("no json here")


def test_a_second_object_after_the_first_is_ignored_not_fatal():
    """A real reply appended a second object and killed the whole cycle."""
    assert _extract_json('{"a": 1}\n{"b": 2}') == {"a": 1}
    assert _extract_json('{"a": {"b": 1}} and here is why') == {"a": {"b": 1}}
    with pytest.raises(DraftError, match="not valid JSON"):
        _extract_json('{"a": 1')


def test_prompt_excludes_evaluator_material():
    messages = build_messages(decision(TARGET_BEHAVIOR), experience=EXPERIENCE, trace=[{"tool": "probe"}],
                              asset_state={"bundles": 3}, evidence_ids=EVIDENCE_IDS, source_refs=SOURCE_REFS)
    blob = json.dumps(messages, ensure_ascii=False).lower()
    for forbidden in ("holdout", "safety_eval", "judge", "rubric", "grader"):
        assert forbidden not in blob
    assert messages[0]["role"] == "system"
    assert "untrusted data" in messages[0]["content"]
    assert "may not propose new tool permissions" in messages[0]["content"]


def test_prompt_offers_only_the_decided_target_shape():
    memory_messages = build_messages(decision(TARGET_MEMORY), experience=EXPERIENCE)
    assert '"operation":"ADD|UPDATE"' in memory_messages[0]["content"]
    behavior_messages = build_messages(decision(TARGET_BEHAVIOR), experience=EXPERIENCE)
    assert '"subtype":"prompt|model_policy"' in behavior_messages[0]["content"]
    assert json.loads(behavior_messages[1]["content"])["behavior_surfaces"]


def test_the_offered_behavior_surfaces_match_the_release_adapters():
    """The prompt must not offer a subtype the adapter cannot release."""
    from app.learning_contract import BEHAVIOR_RELEASABLE_SUBTYPES
    from app.learning_targets import BEHAVIOR_SURFACE_PATHS

    assert set(BEHAVIOR_RELEASABLE_SUBTYPES) == set(BEHAVIOR_SURFACE_PATHS)
    payload = json.loads(build_messages(decision(TARGET_BEHAVIOR), experience=EXPERIENCE)[1]["content"])
    assert set(payload["behavior_surfaces"]) == set(BEHAVIOR_SURFACE_PATHS)
