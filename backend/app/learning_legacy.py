"""The pre-V3 target-specific branches, kept reachable during the migration.

V3 §63 gives `learning.py` one responsibility — jobs, leases, budget, scheduling
and recovery — and moves the `if source_kind == ...` business branches out. These
are those branches: deterministic extractors written before the Learning
Pipeline existed.

`LearningPipeline` replaces them. They stay reachable from `LearningService` so
the frozen P0 regression baseline (`tests/test_learning_v2.py`) keeps exercising
the behaviour it was pinned to, and V3 §65 step 23 deletes them once the pipeline
is live and the acceptance gates are green.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from .learning import (
    LearningConflict,
    digest,
    encode,
    explicit_constraint,
    needs_constraint_extraction,
    now,
)


class LegacyLearningBranches:
    """Mixin: one apply path per legacy `source_kind`, plus its dispatch."""

    def apply_explicit_memory(self, job):
        if job["source_kind"] != "thread_message":
            with self.db.transaction() as connection:
                self._owned(connection, job)
                self._finish(connection, job, "NO_CHANGE", reason="No supported deterministic extraction")
            return
        with self.db.connection() as connection:
            row = connection.execute("SELECT m.*,t.owner_id,t.project_id,t.deleted_at FROM thread_messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=?",
                                     (job["source_id"],)).fetchone()
        if row is None or row["owner_id"] != job["owner_id"] or row["role"] != "user" or row["deleted_at"] or digest(row["content"]) != job["source_hash"]:
            raise LearningConflict("learning source invalidated")
        constraint = explicit_constraint(row["content"])
        if constraint is None and needs_constraint_extraction(row["content"]):
            extractor = getattr(self, "constraint_extractor", None)
            if extractor is not None:
                root_id = self.dispatch(job)
                constraint = extractor(job, row["content"], root_id)
        applicability = json.loads(job["checkpoint_json"]).get("applicability", {})
        if constraint is None or (constraint["project_only"] and not row["project_id"]) or (constraint.get("temporary") and not applicability):
            with self.db.transaction() as connection:
                self._owned(connection, job)
                self._finish(connection, job, "NO_CHANGE", reason="Explicit scope or supported constraint missing")
            return
        scope_type = "project" if constraint["project_only"] else "user"
        scope_id = row["project_id"] if constraint["project_only"] else ""
        previous = None
        for item in self.history(job["owner_id"]):
            metadata = item["checkpoint"]
            if metadata.get("setting") == constraint["setting"] and metadata.get("scope") == [scope_type, scope_id] and metadata.get("applicability", {}) == applicability:
                previous = metadata
                break
        target = self.memory.get(previous["entry_id"], job["owner_id"]) if previous else None
        if target is not None and (target.status != "ACTIVE" or target.revision_id != previous["revision_id"] or previous["source_created_at"] >= row["created_at"]):
            raise LearningConflict("newer correction or forgotten memory takes precedence")
        proposal = self.memory.propose(owner_id=job["owner_id"], operation="UPDATE" if target else "ADD", kind="constraint",
                                       scope_type=scope_type, scope_id=scope_id,
                                       content=row["content"] + ("\n[applicability: " + encode(applicability) + "]" if applicability else ""), confidence=1,
                                       evidence_refs=[{"source_type": "thread_message", "source_id": row["id"]}],
                                       idempotency_key=job["id"] + ":proposal", source_thread_id=row["thread_id"],
                                       target_entry_id=target.id if target else None, base_revision_id=target.revision_id if target else None)
        with self.db.transaction() as connection:
            current, policy = self._owned(connection, job)
            if "memory" not in policy["config"]["allowed_assets"]:
                self._finish(connection, job, "REJECTED", reason="Memory is outside learning authority")
                return
            applied = connection.execute("SELECT checkpoint_json FROM learning_jobs WHERE owner_id=? AND status='APPLIED' ORDER BY created_at DESC,id DESC", (job["owner_id"],)).fetchall()
            latest = next((item for item in (json.loads(record[0]) for record in applied)
                           if item.get("setting") == constraint["setting"] and item.get("scope") == [scope_type, scope_id] and item.get("applicability", {}) == applicability), None)
            if latest != previous:
                raise LearningConflict("concurrent setting correction won adoption")
            # Source is rechecked under the same transaction as the decision.
            evidence = self.memory._resolve_evidence_item(connection, job["owner_id"], scope_type, scope_id, "thread_message", row["id"], source_thread_id=row["thread_id"], source_run_id=None)
            source = connection.execute("SELECT content FROM thread_messages WHERE id=?", (row["id"],)).fetchone()
            if digest(source["content"]) != current["source_hash"]:
                raise LearningConflict("learning source changed")
            accepted = self.memory.decide_proposal(proposal.id, job["owner_id"], True, job["id"] + ":adopt", expected_version=proposal.version,
                                                   authority=f"policy:{job['owner_id']}:{policy['version']}", connection=connection)
            revision = connection.execute("SELECT entry_id FROM memory_revisions WHERE id=?", (accepted.accepted_revision_id,)).fetchone()
            connection.execute("UPDATE memory_entries SET applicability_json=? WHERE id=?", (encode(applicability), revision["entry_id"]))
            if target is not None:
                pins = connection.execute("SELECT pin_invocation_id FROM memory_context_pin_items WHERE source_type='revision' AND source_id=?", (target.revision_id,)).fetchall()
                for pin in pins:
                    connection.execute("UPDATE memory_context_pins SET invalidated_at=?,invalidation_reason='explicit_correction' WHERE model_invocation_id=?", (now(), pin[0]))
                    connection.execute("DELETE FROM memory_context_pin_payloads WHERE pin_invocation_id=?", (pin[0],))
            metadata = {"setting": constraint["setting"], "value": constraint["value"], "scope": [scope_type, scope_id],
                        "entry_id": revision["entry_id"], "revision_id": accepted.accepted_revision_id, "source_created_at": row["created_at"], "applicability": applicability}
            self._finish(connection, job, "APPLIED", checkpoint=metadata, changes=[{
                "asset_type": "memory", "before": target.revision_id if target else None, "after": accepted.accepted_revision_id,
                "proposal_id": proposal.id, "claim_basis": "explicit_user", "adoption": "ACTIVE", "effect": "UNKNOWN",
                "source_id": row["id"], "root_id": evidence.independence_key, "authority": f"policy:{policy['version']}",
            }])
        self.memory.project(job["owner_id"])
        if target is not None:
            self.assets.revoke(job["owner_id"], "revision", target.revision_id, "explicit_correction")

    def learn_research_policy(self, job):
        from .behavior import BehaviorBundleService
        with self.db.connection() as connection:
            source = connection.execute("SELECT j.*,t.project_id FROM research_jobs j JOIN threads t ON t.id=j.thread_id WHERE j.id=? AND t.owner_id=? AND t.deleted_at IS NULL", (job["source_id"], job["owner_id"])).fetchone()
            if source is None or source["status"] != "COMPLETED" or digest(source["topic"]) != job["source_hash"]:
                raise LearningConflict("research source invalidated")
            observations = connection.execute("SELECT j.id,j.topic,j.retry_of_job_id,t.project_id,e.event_id,e.data_json FROM research_jobs j JOIN threads t ON t.id=j.thread_id "
                                              "JOIN thread_events e ON e.turn_id=j.source_turn_id WHERE t.owner_id=? AND t.deleted_at IS NULL AND j.status='COMPLETED' "
                                              "AND e.type='task_policy.research_decision' ORDER BY j.created_at,j.id", (job["owner_id"],)).fetchall()
        samples = {}
        for row in observations:
            data = json.loads(row["data_json"])
            if row["project_id"] == source["project_id"] and row["retry_of_job_id"] is None and data.get("initial_coverage") is True and not data.get("optional_exploration_skipped"):
                samples.setdefault(digest(row["topic"]), row)
        scope = ["project", source["project_id"]] if source["project_id"] else ["user", ""]
        with self.db.transaction() as connection:
            _, authority = self._owned(connection, job)
            if "task_policy" not in authority["config"]["allowed_assets"]:
                raise LearningConflict("research policy outside learning authority")
            if len(samples) < 3:
                self._finish(connection, job, "NO_CHANGE", reason="Need three distinct completed research tasks with initial coverage")
                return
        bundles = BehaviorBundleService(self.db)
        base = bundles.active("stable")
        target = bundles.ensure({**base.manifest, "task_policy": {"schema_version": 1, "research_stop_condition": "coverage_satisfied"}})
        with self.db.transaction() as connection:
            _, authority = self._owned(connection, job)
            prior = connection.execute("SELECT change_set_json FROM learning_jobs WHERE owner_id=? AND status IN ('APPLIED','SUSPENDED')", (job["owner_id"],)).fetchall()
            if any(change.get("purpose") == "research" and change.get("scope") == scope for row in prior for change in json.loads(row[0])):
                self._finish(connection, job, "NO_CHANGE", reason="Research stopping strategy already evaluated in this scope")
                return
            if connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()[0] != base.id:
                raise LearningConflict("research baseline changed")
            self._finish(connection, job, "APPLIED", checkpoint={"research_ids": [row["id"] for row in samples.values()],
                                                                 "contract": "initial-coverage-v1", "required_delivery": "all_sections_and_final_audit", "effect": "UNKNOWN"}, changes=[{
                "asset_type": "task_policy", "before": base.id, "after": target.id, "scope": scope, "role": "researcher", "purpose": "research",
                "adoption": "TRIAL", "effect": "UNKNOWN", "claim_basis": "observed_execution", "authority": f"policy:{authority['version']}",
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=authority["config"]["trial_days"])).isoformat(),
            }])

    def learn_workflow(self, job):
        from .learning_workflow import METHOD, matches, package
        from .skill_platform import SkillPlatform, _digest as skill_digest

        platform = SkillPlatform(self.db, self.skill_root, job["owner_id"])
        with self.db.connection() as connection:
            sources = connection.execute(
                "SELECT f.id,f.note,a.program_id,p.source_plan_content_hash,t.project_id FROM goal_action_feedback f "
                "JOIN goal_actions a ON a.id=f.action_id JOIN goal_programs p ON p.id=a.program_id JOIN threads t ON t.id=p.source_thread_id "
                "WHERE f.owner_id=? AND p.deleted_at IS NULL AND a.status='COMPLETED' AND f.kind='method_success' AND f.note=? ORDER BY f.created_at,f.id",
                (job["owner_id"], METHOD)).fetchall()
        source = next((item for item in sources if item["id"] == job["source_id"]), None)
        if source is None or digest(source["note"]) != job["source_hash"]:
            raise LearningConflict("method feedback source invalidated")
        support = [item for item in sources if item["project_id"] == source["project_id"]]
        scope = ["project", source["project_id"]] if source["project_id"] else ["user", ""]
        with self.db.transaction() as connection:
            _, policy = self._owned(connection, job)
            if "skill" not in policy["config"]["allowed_assets"]:
                raise LearningConflict("skill outside learning authority")
            if len({item["program_id"] for item in support}) < 2 or len({item["source_plan_content_hash"] for item in support}) < 2:
                self._finish(connection, job, "NO_CHANGE", reason="Method requires independent completed programs and distinct source plans")
                return
            prior = connection.execute("SELECT change_set_json FROM learning_jobs WHERE owner_id=? AND status IN ('APPLIED','SUSPENDED')", (job["owner_id"],)).fetchall()
            if any(change.get("asset_type") == "skill" and change.get("scope") == scope for row in prior for change in json.loads(row[0])):
                self._finish(connection, job, "NO_CHANGE", reason="Method already considered in this scope; no automatic resurrection")
                return
        candidate = platform.store_candidate(package("1.0." + str(int(job["id"][-8:], 16))), job_id=job["id"])
        checks = {
            "new_tasks": all(matches(text) for text in ("制定线性代数学习计划", "安排英语听力练习")),
            "counterexamples": not any(matches(text) for text in ("查询今天的天气", "不需要练习，直接回答")),
            "no_tool_grants": candidate["kind"] == "instruction_only" and not candidate["requested_tools"] and not candidate["connectors"],
            "output_and_exit_contract": "输出：" in candidate["content"] and "退出：" in candidate["content"],
        }
        with self.db.transaction() as connection:
            _, policy = self._owned(connection, job)
            if not all(checks.values()):
                self._finish(connection, job, "REJECTED", checkpoint={"checks": checks}, reason="Workflow validation failed")
                return
            prior = connection.execute("SELECT change_set_json FROM learning_jobs WHERE owner_id=? AND status IN ('APPLIED','SUSPENDED')", (job["owner_id"],)).fetchall()
            if any(change.get("asset_type") == "skill" and change.get("scope") == scope for row in prior for change in json.loads(row[0])):
                raise LearningConflict("another method decision already owns this scope")
            for item in support:
                if connection.execute("SELECT 1 FROM goal_action_feedback f JOIN goal_actions a ON a.id=f.action_id JOIN goal_programs p ON p.id=a.program_id "
                                      "WHERE f.id=? AND f.owner_id=? AND f.kind='method_success' AND f.note=? AND p.deleted_at IS NULL",
                                      (item["id"], job["owner_id"], METHOD)).fetchone() is None:
                    raise LearningConflict("method evidence changed before adoption")
            # This enables instructions, not external tool authority.
            connection.execute("UPDATE skill_versions SET status='ENABLED' WHERE id=? AND status='INSTALLED'", (candidate["version_id"],))
            connection.execute("UPDATE skills SET status='ENABLED',default_version_id=?,updated_at=? WHERE id=? AND status<>'UNINSTALLED'",
                               (candidate["version_id"], now(), candidate["skill_id"]))
            connection.execute("INSERT INTO skill_grants(id,owner_id,skill_version_id,granted_tools_json,grant_digest,status,created_at,updated_at) "
                               "VALUES (?,?,?,'[]',?,'ACTIVE',?,?) ON CONFLICT(owner_id,skill_version_id) DO NOTHING",
                               ("skill_grant_" + uuid.uuid4().hex, job["owner_id"], candidate["version_id"], skill_digest([]), now(), now()))
            platform._event(connection, candidate["skill_id"], candidate["version_id"], "skill.policy_adopted", {"job_id": job["id"], "checks": checks},
                            job["id"] + ":skill-adopt", actor=f"policy:{policy['version']}")
            self._finish(connection, job, "APPLIED", checkpoint={"checks": checks, "feedback_ids": [item["id"] for item in support],
                                                                "root_ids": list(dict.fromkeys(item["program_id"] for item in support))}, changes=[{
                "asset_type": "skill", "after": candidate["version_id"], "scope": scope, "name": candidate["name"],
                "adoption": "TRIAL", "effect": "UNKNOWN", "claim_basis": "explicit_user",
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=policy["config"]["trial_days"])).isoformat(),
                "evidence_scope": "user_method_success_and_applicability_checks; downstream_effect_unproven",
            }])

    def adopt_prompt(self, job):
        from .evolution import EvolutionConflict
        evolution = getattr(self, "evolution", None)
        if evolution is None:
            raise LearningConflict("prompt evaluator unavailable")
        try:
            with self.db.transaction() as connection:
                _, policy = self._owned(connection, job)
                if "prompt" not in policy["config"]["allowed_assets"]:
                    raise LearningConflict("prompt is outside learning authority")
                candidate = evolution.get_candidate(job["source_id"], job["owner_id"])
                if candidate["proposed_digest"] != job["source_hash"] or not candidate.get("release_contract_version"):
                    raise LearningConflict("candidate digest or production evaluation contract missing")
                evaluation = evolution.get_evaluation(candidate["current_evaluation_id"])
                from .research.delivery import TRANSFORM_VERSION
                if evaluation.get("metrics", {}).get("delivery_transform_version") != TRANSFORM_VERSION:
                    raise LearningConflict("evaluation did not use the current production delivery transform")
                stable = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
                if stable is None or stable[0] != candidate["base_bundle_id"]:
                    raise LearningConflict("candidate baseline changed")
                active = connection.execute("SELECT change_set_json FROM learning_jobs WHERE owner_id=? AND status='APPLIED'", (job["owner_id"],)).fetchall()
                if any(change.get("asset_type") == "prompt" and change.get("adoption") in {"TRIAL", "ACTIVE"}
                       for row in active for change in json.loads(row[0])):
                    raise LearningConflict("another prompt experiment is active")
                expiry = (datetime.now(timezone.utc) + timedelta(days=policy["config"]["trial_days"])).isoformat()
                authority = f"policy:{job['owner_id']}:{policy['version']}"
                decision = evolution.approve(candidate["id"], expected_version=candidate["version"], evaluation_id=evaluation["id"],
                                             candidate_digest=candidate["proposed_digest"], evaluation_report_digest=evaluation["report_digest"],
                                             permission_diff_digest=candidate["permission_diff_digest"], target_bundle_digest=candidate["target_bundle_digest"],
                                             expires_at=expiry, actor=authority, idempotency_key=job["id"] + ":adopt", owner_id=job["owner_id"], connection=connection)
                self._finish(connection, job, "APPLIED", checkpoint={"experience_ids": candidate["experience_ids"]}, changes=[{
                    "asset_type": "prompt", "before": candidate["base_bundle_id"], "after": candidate["target_bundle_id"],
                    "candidate_id": candidate["id"], "decision_id": decision["id"], "evaluation_id": evaluation["id"],
                    "adoption": "TRIAL", "effect": "UNKNOWN", "claim_basis": "observed_execution",
                    "role": "researcher", "purpose": "write_research_section", "expires_at": expiry, "authority": authority,
                }])
        except EvolutionConflict as exc:
            raise LearningConflict(str(exc)) from exc

    def learn_estimation(self, job):
        """Learn estimation bias from measured actions, never from model self-ratings."""
        from statistics import median
        from .behavior import BehaviorBundleService
        from .task_policy import calibrate_minutes

        with self.db.connection() as connection:
            review = connection.execute("SELECT r.*,t.project_id FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id "
                                        "JOIN threads t ON t.id=p.source_thread_id WHERE r.id=? AND r.owner_id=? AND p.deleted_at IS NULL",
                                        (job["source_id"], job["owner_id"])).fetchone()
            if review is None or review["source_hash"] != job["source_hash"] or review["status"] != "COMPLETED" or review["evidence_stale"]:
                raise LearningConflict("review source invalidated")
            rows = connection.execute(
                "SELECT a.id,a.program_id,a.estimated_minutes,f.actual_minutes,f.id feedback_id,f.created_at,t.project_id,p.source_plan_content_hash "
                "FROM goal_actions a JOIN goal_action_feedback f ON f.action_id=a.id JOIN goal_programs p ON p.id=a.program_id "
                "JOIN threads t ON t.id=p.source_thread_id WHERE p.owner_id=? AND p.deleted_at IS NULL AND a.status='COMPLETED' "
                "AND f.actual_minutes IS NOT NULL AND f.actual_minutes>0 AND f.created_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM goal_action_feedback newer WHERE newer.action_id=f.action_id AND "
                "(newer.created_at>f.created_at OR (newer.created_at=f.created_at AND newer.id>f.id))) "
                "ORDER BY f.created_at,a.id", (job["owner_id"], review["updated_at"])).fetchall()
            stable = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
        samples = {}
        for row in rows:
            if row["project_id"] == review["project_id"] and row["estimated_minutes"] > 0:
                samples.setdefault(row["program_id"], row)
        with self.db.transaction() as connection:
            _, policy = self._owned(connection, job)
            if "task_policy" not in policy["config"]["allowed_assets"]:
                raise LearningConflict("task_policy is outside learning authority")
            if len(samples) < 3 or len({row["source_plan_content_hash"] for row in samples.values()}) < 3 or stable is None:
                self._finish(connection, job, "NO_CHANGE", reason="Need measured outcomes from three independent programs")
                return
            values = list(samples.values())
            train, holdout = values[:-1], values[-1]
            multiplier = min(3.0, max(1.0, round(median(row["actual_minutes"] / row["estimated_minutes"] for row in train), 2)))
            baseline_error = abs(holdout["estimated_minutes"] - holdout["actual_minutes"])
            candidate_error = abs(calibrate_minutes(holdout["estimated_minutes"], multiplier) - holdout["actual_minutes"])
            if multiplier == 1 or candidate_error >= baseline_error:
                self._finish(connection, job, "NO_CHANGE", reason="Held-out measured estimation error did not improve")
                return
            existing = connection.execute("SELECT change_set_json FROM learning_jobs WHERE owner_id=? AND status='APPLIED'", (job["owner_id"],)).fetchall()
            scope = ["project", review["project_id"]] if review["project_id"] else ["user", ""]
            if any(change.get("asset_type") == "task_policy" and change.get("scope") == scope for row in existing for change in json.loads(row[0])):
                self._finish(connection, job, "NO_CHANGE", reason="An estimation trial already occupies this scope")
                return
            # Bundle content is persisted below outside this transaction. Retain
            # the evaluation checkpoint before adoption so a restart can audit it.
            checkpoint = {"multiplier": multiplier, "scope": scope, "base_bundle_id": stable[0],
                          "train_roots": [row["program_id"] for row in train], "holdout_root": holdout["program_id"],
                          "feedback_ids": [row["feedback_id"] for row in values], "samples_digest": digest([dict(row) for row in values]),
                          "baseline_error": baseline_error, "candidate_error": candidate_error, "consumer": "calibrate_minutes-v1"}
            connection.execute("UPDATE learning_jobs SET checkpoint_json=?,updated_at=? WHERE id=? AND lease_token=?", (encode(checkpoint), now(), job["id"], job["lease_token"]))
        bundles = BehaviorBundleService(self.db)
        base = bundles.get(stable[0])
        manifest = base.manifest
        manifest["task_policy"] = {"schema_version": 1, "estimate_multiplier": multiplier}
        target = bundles.ensure(manifest)
        with self.db.transaction() as connection:
            _, policy = self._owned(connection, job)
            current_base = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
            if current_base[0] != base.id:
                raise LearningConflict("estimation baseline changed")
            competing = connection.execute("SELECT change_set_json FROM learning_jobs WHERE owner_id=? AND status IN ('APPLIED','SUSPENDED')", (job["owner_id"],)).fetchall()
            if any(change.get("asset_type") == "task_policy" and change.get("scope") == scope for row in competing for change in json.loads(row[0])):
                raise LearningConflict("another estimation decision already owns this scope")
            self._finish(connection, job, "APPLIED", checkpoint=checkpoint, changes=[{
                "asset_type": "task_policy", "before": base.id, "after": target.id, "scope": scope,
                "role": "planner", "purpose": "compile_goal_program", "claim_basis": "observed_execution",
                "adoption": "TRIAL", "effect": "UNKNOWN", "authority": f"policy:{policy['version']}",
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=policy["config"]["trial_days"])).isoformat(),
            }])

def execute_legacy(service, job):
    """The pre-V3 `if source_kind == ...` dispatch, unchanged.

    `service` is the `LearningService` the branches are mixed into, so every
    `self`-bound helper (leases, dispatch, memory, assets) stays available.
    """
    if job["source_kind"] == "prompt_cycle":
        service.prompt_learning.run(job)
    elif job["source_kind"] == "behavior_candidate":
        service.adopt_prompt(job)
    elif job["source_kind"] == "goal_review":
        service.learn_estimation(job)
    elif job["source_kind"] == "method_feedback":
        service.learn_workflow(job)
    elif job["source_kind"] == "research_result":
        service.learn_research_policy(job)
    else:
        service.apply_explicit_memory(job)
