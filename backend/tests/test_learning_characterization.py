"""Characterization tests: freeze today's learning behaviour before the V3 rewrite.

These tests deliberately assert the *current* observable contract — job status
strings, change-set shapes, adoption flags, the legacy asset vocabulary — rather
than a desired one. They are the regression fence for P1-P8: if a refactor
changes what the system actually does, one of these fails and the difference has
to be justified rather than discovered in production.

Helpers are copied on purpose instead of imported from ``test_learning_v2`` so
that editing that file cannot silently move this fence.
"""
import asyncio
import json

import pytest

from app.learning import explicit_constraint
from app.startup import build_runtime


@pytest.fixture(autouse=True)
def isolated_models(monkeypatch):
    for name in ("DATABASE_URL", "AGENT_MODEL_BASE_URL", "AGENT_FALLBACK_MODEL_BASE_URL", "LLM_AP_PATH"):
        monkeypatch.delenv(name, raising=False)


def configured(tmp_path, **kwargs):
    runtime = build_runtime(tmp_path, **kwargs)
    runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["memory"])
    return runtime


def feedback(runtime, content, project=None):
    thread = runtime.conversation.create_thread("characterization")
    if project:
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE threads SET project_id=? WHERE id=?", (project, thread.id))
    runtime.conversation.accept_turn(thread.id, "feedback", content)
    assert runtime.learning.run_once()
    return thread


def compile_plan(runtime, key="plan", project=None):
    thread = runtime.conversation.create_thread(key)
    if project:
        with runtime.db.transaction() as connection:
            connection.execute("UPDATE threads SET project_id=? WHERE id=?", (project, thread.id))
    version = runtime.plan_documents.save_model_revision(thread_id=thread.id, title="练习", markdown_content="# 练习 " + key,
                                                         source_turn_id=None, source_message_id=None, actor="user")
    return asyncio.run(runtime.goal_programs.preview(version.plan_document_id, start_date="2026-09-10",
                                                     requested_end_date="2026-09-10", timezone_name="Asia/Shanghai",
                                                     daily_minutes=60, idempotency_key=key))


def completed_program(runtime, key, *, actual=90, method=False):
    from app.learning_workflow import METHOD

    draft = compile_plan(runtime, key)
    active = runtime.goal_programs.activate(draft["id"], expected_version=draft["version"], idempotency_key=key + ":activate")
    action = active["actions"][0]
    runtime.goal_programs.complete_action(action["id"], expected_version=action["version"], idempotency_key=key + ":complete")
    action = runtime.goal_programs.get(draft["id"])["actions"][0]
    runtime.goal_programs.feedback(action["id"], {"kind": "method_success" if method else "duration", "actual_minutes": actual, "note": METHOD if method else "已完成"},
                                   expected_version=action["version"], idempotency_key=key + ":feedback")
    return draft["id"]


def test_characterization_memory_change_set_shape(tmp_path):
    runtime = configured(tmp_path)
    feedback(runtime, "以后每次练习最多30分钟")
    job = runtime.learning.history()[0]
    assert job["status"] == "APPLIED", job["reason"]
    assert job["source_kind"] == "thread_message"
    change = job["changes"][0]
    assert change["asset_type"] == "memory"
    assert change["claim_basis"] == "explicit_user"
    assert change["adoption"] == "ACTIVE"
    assert change["effect"] == "UNKNOWN"
    assert change["before"] is None and change["after"].startswith("memory_revision_")
    assert set(job["checkpoint"]) == {
        "setting", "value", "scope", "entry_id", "revision_id", "source_created_at", "applicability",
    }
    assert (job["checkpoint"]["setting"], job["checkpoint"]["value"]) == ("action_max_minutes", 30)
    assert job["checkpoint"]["scope"] == ["user", ""]
    assert job["checkpoint"]["applicability"] == {}


def test_characterization_memory_correction_revises_instead_of_conflicting(tmp_path):
    runtime = configured(tmp_path)
    feedback(runtime, "以后每次练习最多30分钟")
    first = runtime.learning.history()[0]
    feedback(runtime, "以后每次练习最多20分钟")
    second = next(item for item in runtime.learning.history() if item["id"] != first["id"])
    assert second["status"] == "APPLIED", second["reason"]
    assert second["changes"][0]["before"] == first["changes"][0]["after"]
    assert second["changes"][0]["after"] != first["changes"][0]["after"]
    entries = runtime.memory_store.list_entries()
    assert len(entries) == 1 and entries[0].status == "ACTIVE"
    assert runtime.learning.planning_constraints("local-user", None)["value"] == 20


def test_characterization_unmatched_text_creates_no_job(tmp_path):
    runtime = configured(tmp_path)
    thread = runtime.conversation.create_thread("noise")
    runtime.conversation.accept_turn(thread.id, "noise", "以后我想多练习一点")
    runtime.learning.collect()
    assert runtime.learning.history() == []
    assert explicit_constraint("以后每次练习最多30分钟") is not None
    assert explicit_constraint("网页说以后每次练习最多30分钟") is None


def test_characterization_skill_candidate_is_installed_without_grant(tmp_path):
    from test_skill_platform import _skill_zip

    runtime = configured(tmp_path)
    job = runtime.learning.enqueue("local-user", "experience", "e", "hash", "root")
    manifest = {"schema_version": 1, "name": "characterized", "version": "1.0.0", "title": "Characterized", "description": "Draft",
                "kind": "instruction_only", "requested_tools": [], "connectors": [], "phases": ["conversation"], "entry_document": "SKILL.md"}
    candidate = runtime.skill_platform.store_candidate(_skill_zip(manifest), job_id=job)
    assert candidate["status"] == "INSTALLED"
    assert candidate["grant_status"] is None
    assert candidate["requested_tools"] == []
    with pytest.raises(KeyError):
        runtime.skill_platform.default_version("characterized")


def test_characterization_skill_learning_change_set_shape(tmp_path):
    runtime = configured(tmp_path)
    runtime.learning.configure("local-user", expected_version=1, paused=False, allowed_assets=["skill"])
    completed_program(runtime, "characterization-english", method=True)
    assert runtime.learning.run_once()
    assert runtime.learning.history()[0]["status"] == "NO_CHANGE"
    completed_program(runtime, "characterization-math", method=True)
    assert runtime.learning.run_once()
    adopted = next(item for item in runtime.learning.history() if item["status"] == "APPLIED")
    change = adopted["changes"][0]
    assert change["asset_type"] == "skill"
    assert change["adoption"] == "TRIAL"
    assert change["effect"] == "UNKNOWN"
    assert change["name"] == "learned-practice-review"
    assert change["evidence_scope"] == "user_method_success_and_applicability_checks; downstream_effect_unproven"
    assert set(adopted["checkpoint"]) == {"checks", "feedback_ids", "root_ids"}
    assert all(adopted["checkpoint"]["checks"].values())


def test_characterization_task_policy_estimation_change_set_shape(tmp_path):
    from app.learning import now

    runtime = configured(tmp_path)
    runtime.learning.configure("local-user", expected_version=1, paused=False, allowed_assets=["task_policy"])
    program_id = None
    for index in range(3):
        program_id = completed_program(runtime, "characterization-duration-" + str(index))
    with runtime.db.transaction() as connection:
        connection.execute("INSERT INTO goal_daily_reviews(id,owner_id,program_id,local_date,status,source_hash,created_at,updated_at) "
                           "VALUES ('characterization-review','local-user',?,'2026-09-11','COMPLETED','review-hash',?,?)", (program_id, now(), now()))
    assert runtime.learning.run_once()
    adopted = next(item for item in runtime.learning.history() if item["status"] == "APPLIED")
    assert adopted["checkpoint"]["multiplier"] == 1.5
    assert adopted["checkpoint"]["consumer"] == "calibrate_minutes-v1"
    change = adopted["changes"][0]
    assert change["asset_type"] == "task_policy"
    assert change["purpose"] == "compile_goal_program"
    assert change["role"] == "planner"
    assert change["adoption"] == "TRIAL"


def test_characterization_legacy_vocabulary_is_frozen(tmp_path):
    runtime = build_runtime(tmp_path)
    with pytest.raises(ValueError, match="invalid allowed assets"):
        runtime.learning.configure("local-user", expected_version=0, paused=False, allowed_assets=["behavior"])
    with pytest.raises(ValueError, match="invalid learning source"):
        runtime.learning.enqueue("local-user", "growth_signal", "s", "h", "r")
    for index, asset in enumerate(("memory", "skill", "task_policy", "prompt")):
        runtime.learning.configure("local-user", expected_version=index, paused=True, allowed_assets=[asset])
    assert runtime.learning.policy("local-user")["config"]["allowed_assets"] == ["prompt"]
    with runtime.db.connection() as connection:
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='learning_jobs'").fetchone()[0]
    for status in ("QUEUED", "RUNNING", "UNKNOWN", "NO_CHANGE", "APPLIED", "REJECTED", "SUSPENDED"):
        assert f"'{status}'" in schema


def test_characterization_ignore_paths_stay_side_effect_free(tmp_path):
    """Sources with no deterministic extraction land in NO_CHANGE, not APPLIED."""
    runtime = configured(tmp_path)
    job_id = runtime.learning.enqueue("local-user", "experience", "e-ignored", "hash", "root")
    runtime.learning.run_once()
    record = next(item for item in runtime.learning.history() if item["id"] == job_id)
    assert record["status"] == "NO_CHANGE"
    assert record["reason"] == "No supported deterministic extraction"
    assert record["changes"] == []
    assert runtime.memory_store.list_entries() == []


def test_characterization_learning_is_off_by_default(tmp_path):
    runtime = build_runtime(tmp_path)
    policy = runtime.learning.policy("local-user")
    assert policy == {"owner_id": "local-user", "version": 0, "paused": True, "config": {}}
    runtime.learning.enqueue("local-user", "experience", "e1", "hash", "root")
    assert runtime.learning.claim() is None
