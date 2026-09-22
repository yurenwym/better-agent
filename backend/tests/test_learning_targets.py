"""Target adapters: Memory proposals, grant-free skills, release-gated behavior."""
import pytest

from app.learning_agent import BehaviorDraft, MemoryDraft, SkillDraft
from app.learning_contract import TARGET_BEHAVIOR, TARGET_MEMORY, TARGET_SKILL
from app.learning_targets import (
    BEHAVIOR_SURFACE_PATHS,
    BehaviorTargetAdapter,
    MemoryTargetAdapter,
    SkillTargetAdapter,
    build_adapters,
)
from app.startup import build_runtime

SECRET_LIKE = "password=hunter2"


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)


def runtime_with_message(tmp_path, content="以后生产数据库不能由 Agent 自动重启，必须人工批准。"):
    runtime = build_runtime(tmp_path)
    thread = runtime.conversation.create_thread("v3")
    runtime.conversation.accept_turn(thread.id, "one", content)
    with runtime.db.connection() as connection:
        message_id = connection.execute(
            "SELECT id FROM thread_messages WHERE thread_id=? AND role='user'", (thread.id,)).fetchone()[0]
    return runtime, message_id


def memory_draft(message_id, **over):
    change = {"operation": "ADD", "kind": "constraint", "scope_type": "user", "scope_id": "",
              "content": "生产数据库不能由 Agent 自动重启，必须人工批准。"}
    change.update(over.pop("change", {}))
    draft = {
        "problem": "用户显式要求生产库不能自动重启",
        "root_cause": "该约束此前没有被持久化",
        "generalizable_lesson": "生产库重启必须人工批准",
        "proposed_change": change,
        "expected_effect": {"future_restarts": "require approval"},
        "risks": ("约束过宽会阻碍正常运维",),
        "evidence_refs": (message_id,),
        "experience_ids": ("experience_1",),
        "source_refs": (message_id,),
        "evidence_digest": "sha256:stub",
    }
    draft.update(over)
    return MemoryDraft(**draft)


def skill_draft(**over):
    change = {"name": "database-timeout-diagnosis", "title": "数据库超时诊断",
              "description": "按固定顺序定位数据库超时。", "kind": "instruction_only",
              "content": "# 数据库超时诊断\n适用：数据库超时。\n1. 检查连接池\n2. 检查慢 SQL\n输出：定位结论与证据。\n退出：任务与数据库超时无关时停止。",
              "requested_tools": [], "connectors": [], "phases": ["conversation"]}
    change.update(over.pop("change", {}))
    draft = {
        "problem": "数据库超时反复出现，每次都重新摸索",
        "root_cause": "缺少固定的排查顺序",
        "generalizable_lesson": "先查连接池，再查慢 SQL",
        "proposed_change": change,
        "expected_effect": {"tool_calls": "fewer"},
        "risks": (),
        "evidence_refs": ("thread_message_1",),
        "experience_ids": ("experience_1",),
        "source_refs": ("thread_message_1",),
        "evidence_digest": "sha256:stub",
    }
    draft.update(over)
    return SkillDraft(**draft)


def record_experiences(runtime, tag="behavior", count=3):
    base = runtime.behavior.active("stable")
    return [runtime.evolution.record_experience(
        task_type="incident_diagnosis", outcome="failure", lineage_group_hash=f"lineage-{tag}-{index}",
        root_task_id=f"root-{tag}-{index}", source_content_hash=f"source-{tag}-{index}",
        runtime_bundle_id=base.id, dataset_partition="DISCOVERY", source_kind="manual",
        source_id=f"job-{tag}-{index}", source_event_id=f"event-{tag}-{index}", signal_type="run_failed",
        severity="error", failure_tags=["premature_root_cause"], provenance="production",
        idempotency_key=f"v3-exp-{tag}-{index}",
    ) for index in range(count)]


def behavior_draft(experience_ids, **over):
    draft = {
        "problem": "只有一条证据就确认根因",
        "root_cause": "缺少证据充分性检查",
        "generalizable_lesson": "确认根因前要求充分证据",
        "proposed_change": {"subtype": "prompt", "surface": "prompts.researcher.write_research_section",
                            "change": {"researcher": {"write_research_section": {
                                "evidence_statement": "确认根因前必须给出至少两条独立证据。"}}},
                            "rationale": "降低误判"},
        "expected_effect": {"false_root_cause": "down"},
        "risks": ("可能增加交互轮次",),
        "evidence_refs": (),
        "experience_ids": tuple(experience_ids),
        "source_refs": (),
        "evidence_digest": "sha256:stub",
    }
    draft.update(over)
    return BehaviorDraft(**draft)


def adapters(runtime):
    return build_adapters(db=runtime.db, memory=runtime.memory_store, platform=runtime.skill_platform,
                          bundles=runtime.behavior, evolution=runtime.evolution)


# ---------------------------------------------------------------- Memory

def test_memory_adapter_creates_a_proposal_not_an_entry(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = adapters(runtime)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    assert candidate["proposal_id"].startswith("memory_proposal_")
    assert candidate["status"] == "PENDING"
    assert runtime.memory_store.list_entries() == []
    assert runtime.memory_store.get_proposal(candidate["proposal_id"]).evidence_state == "VERIFIED"


def test_memory_adapter_validation_passes_a_grounded_proposal(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = adapters(runtime)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    verdict = adapter.validate_candidate(candidate, owner_id="local-user")
    assert verdict["pass"], verdict["reason"]
    assert all(verdict["checks"].values())


def test_memory_adapter_refuses_secret_like_content(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path, f"以后生产库口令是 {SECRET_LIKE}")
    adapter = adapters(runtime)[TARGET_MEMORY]
    draft = memory_draft(message_id, change={"content": f"生产库口令是 {SECRET_LIKE}"})
    # memory_v2 refuses the content at proposal time; the adapter never sees a candidate.
    with pytest.raises(ValueError, match="secret-like"):
        adapter.create_candidate(draft, owner_id="local-user", job_id="job_secret")
    assert runtime.memory_store.list_proposals() == []


def test_memory_adapter_refuses_evidence_from_another_owner(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = adapters(runtime)[TARGET_MEMORY]
    with pytest.raises(ValueError, match="outside the owner scope"):
        adapter.create_candidate(memory_draft(message_id), owner_id="someone-else", job_id="job_foreign")


def test_memory_promotion_creates_an_entry_and_rollback_archives_it(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    adapter = adapters(runtime)[TARGET_MEMORY]
    candidate = adapter.create_candidate(memory_draft(message_id), owner_id="local-user", job_id="job_memory")
    promoted = adapter.promote(candidate, owner_id="local-user")
    assert promoted["status"] == "ACCEPTED"
    entries = runtime.memory_store.list_entries()
    assert len(entries) == 1 and entries[0].status == "ACTIVE"
    rolled = adapter.rollback({**candidate, "entry_id": entries[0].id}, owner_id="local-user")
    assert rolled["status"] == "ARCHIVED"
    assert runtime.memory_store.get(entries[0].id).status == "ARCHIVED"


# ---------------------------------------------------------------- Skill

def test_skill_adapter_stores_a_candidate_without_enabling_it(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["skill"])
    job_id = runtime.learning.enqueue("local-user", "experience", "e", "hash", "root")
    adapter = adapters(runtime)[TARGET_SKILL]
    candidate = adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=job_id)
    assert candidate["status"] == "INSTALLED"
    assert candidate["grant_status"] is None
    assert candidate["requested_tools"] == []
    verdict = adapter.validate_candidate(candidate, owner_id="local-user")
    assert verdict["pass"], verdict["reason"]
    with pytest.raises(KeyError):
        runtime.skill_platform.default_version(candidate["name"])


@pytest.mark.parametrize("change,check", [
    ({"content": "# 无退出条件\n输出：x"}, "exit_condition"),
    ({"content": "适用：x\n输出：x\n退出：y\n```python\nimport os\n```"}, "no_executable_code"),
    ({"content": "输出：x\n退出：y"}, "trigger_condition"),
])
def test_skill_adapter_rejects_unsafe_or_incomplete_skills(tmp_path, change, check):
    runtime, _ = runtime_with_message(tmp_path)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["skill"])
    job_id = runtime.learning.enqueue("local-user", "experience", "e", "hash", "root")
    adapter = adapters(runtime)[TARGET_SKILL]
    candidate = adapter.create_candidate(skill_draft(change=change), owner_id="local-user", job_id=job_id)
    verdict = adapter.validate_candidate(candidate, owner_id="local-user")
    assert not verdict["pass"] and verdict["checks"][check] is False


def test_skill_promotion_enables_without_granting_tools(tmp_path):
    runtime, _ = runtime_with_message(tmp_path)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["skill"])
    job_id = runtime.learning.enqueue("local-user", "experience", "e", "hash", "root")
    adapter = adapters(runtime)[TARGET_SKILL]
    candidate = adapter.create_candidate(skill_draft(), owner_id="local-user", job_id=job_id)
    promoted = adapter.promote(candidate, owner_id="local-user")
    assert promoted["status"] == "ENABLED" and promoted["granted_tools"] == []
    version = runtime.skill_platform.version(candidate["version_id"])
    assert version["status"] == "ENABLED" and version["granted_tools"] == []
    rolled = adapter.rollback(candidate, owner_id="local-user")
    assert rolled["status"] == "DISABLED"
    assert runtime.skill_platform.version(candidate["version_id"])["status"] != "ENABLED"


# ---------------------------------------------------------------- Behavior

def test_behavior_adapter_builds_a_target_bundle_and_candidate(tmp_path):
    runtime = build_runtime(tmp_path)
    items = record_experiences(runtime)
    adapter = adapters(runtime)[TARGET_BEHAVIOR]
    candidate = adapter.create_candidate(behavior_draft([item["id"] for item in items]),
                                         owner_id="local-user", job_id="job_behavior")
    assert candidate["subtype"] == "prompt"
    assert candidate["base_bundle_id"] != candidate["target_bundle_id"]
    assert candidate["permission_diff"] == {"added": [], "removed": []}
    # `EvolutionService` diffs manifest keys shallowly, so `prompts` is the changed key.
    assert candidate["diff"]["prompts"]["researcher"]["write_research_section"]["evidence_statement"]
    verdict = adapter.validate_candidate(candidate, owner_id="local-user")
    assert verdict["pass"], verdict["reason"]


def test_behavior_adapter_refuses_frozen_surfaces(tmp_path):
    runtime = build_runtime(tmp_path)
    items = record_experiences(runtime)
    adapter = adapters(runtime)[TARGET_BEHAVIOR]
    draft = behavior_draft([item["id"] for item in items],
                           proposed_change={"subtype": "prompt", "surface": "prompts", "rationale": "x",
                                            "change": {"permissions": ["shell"]}})
    with pytest.raises(ValueError, match="may not modify"):
        adapter.create_candidate(draft, owner_id="local-user", job_id="job_frozen")


def test_behavior_adapter_refuses_an_unsupported_subtype(tmp_path):
    runtime = build_runtime(tmp_path)
    items = record_experiences(runtime)
    adapter = adapters(runtime)[TARGET_BEHAVIOR]
    draft = behavior_draft([item["id"] for item in items],
                           proposed_change={"subtype": "task_policy", "surface": "task_policy", "rationale": "x",
                                            "change": {"task_policy": {"schema_version": 1}}})
    with pytest.raises(ValueError, match="no release adapter"):
        adapter.create_candidate(draft, owner_id="local-user", job_id="job_unsupported")


def test_behavior_adapter_refuses_a_change_that_alters_nothing(tmp_path):
    runtime = build_runtime(tmp_path)
    items = record_experiences(runtime)
    adapter = adapters(runtime)[TARGET_BEHAVIOR]
    draft = behavior_draft([item["id"] for item in items],
                           proposed_change={"subtype": "prompt", "surface": "prompts", "rationale": "x",
                                            "change": {"version": "live-model-v1"}})
    with pytest.raises(ValueError, match="does not alter"):
        adapter.create_candidate(draft, owner_id="local-user", job_id="job_noop")


def test_behavior_adapter_requires_independent_experiences(tmp_path):
    runtime = build_runtime(tmp_path)
    items = record_experiences(runtime, count=1)
    adapter = adapters(runtime)[TARGET_BEHAVIOR]
    from app.evolution import EvolutionGateError

    with pytest.raises(EvolutionGateError, match="three independent"):
        adapter.create_candidate(behavior_draft([item["id"] for item in items]),
                                 owner_id="local-user", job_id="job_thin")


def test_adapter_registry_covers_exactly_the_three_targets(tmp_path):
    runtime = build_runtime(tmp_path)
    registry = adapters(runtime)
    assert set(registry) == {TARGET_MEMORY, TARGET_SKILL, TARGET_BEHAVIOR}
    assert isinstance(registry[TARGET_MEMORY], MemoryTargetAdapter)
    assert isinstance(registry[TARGET_SKILL], SkillTargetAdapter)
    assert isinstance(registry[TARGET_BEHAVIOR], BehaviorTargetAdapter)
    assert set(BEHAVIOR_SURFACE_PATHS) == {"prompt", "model_policy"}


def test_adapters_reject_a_draft_of_the_wrong_type(tmp_path):
    runtime, message_id = runtime_with_message(tmp_path)
    registry = adapters(runtime)
    with pytest.raises(ValueError, match="MemoryDraft"):
        registry[TARGET_MEMORY].create_candidate(skill_draft(), owner_id="local-user", job_id="job_wrong")
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["skill"])
    job_id = runtime.learning.enqueue("local-user", "experience", "e", "hash", "root")
    with pytest.raises(ValueError, match="SkillDraft"):
        registry[TARGET_SKILL].create_candidate(memory_draft(message_id), owner_id="local-user", job_id=job_id)
