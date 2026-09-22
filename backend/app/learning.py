"""Durable, owner-scoped learning jobs. Unknown remote outcomes are never replayed."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from .config import monetary_limits_enabled


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class LearningConflict(ValueError):
    pass


# Deliberately narrow: quoted text, questions and task-local wording do not
# become durable preferences. Unmatched natural language remains NO_CHANGE.
EXPLICIT_MINUTES = re.compile(r"(?:请记住[，,：:]?\s*)?(以后|今后|这个项目|这次计划|本次任务)(?:，|,|\s)*每次(?:练习|学习|训练)?(?:最多|不超过|控制在|上限为|限制为)\s*([0-9]{1,3})\s*分钟(?:以内)?[。！!]?\Z")


def explicit_constraint(text):
    match = EXPLICIT_MINUTES.fullmatch(text.strip())
    if match and 5 <= int(match[2]) <= 180:
        return {"setting": "action_max_minutes", "value": int(match[2]), "project_only": match[1] == "这个项目", "temporary": match[1] in {"这次计划", "本次任务"}}
    return None


def needs_constraint_extraction(text):
    return len(text) <= 4000 and any(word in text for word in ("以后", "今后", "请记住", "这个项目")) and "分钟" in text


# `learning_legacy` imports the helpers above from this module, so this import
# has to come after them and before the class that mixes it in.
from .learning_legacy import LegacyLearningBranches  # noqa: E402


class LearningService(LegacyLearningBranches):
    """Durable, owner-scoped learning jobs (V3 §63: jobs, leases, budget, scheduling, recovery)."""

    def __init__(self, db, memory, costs, *, pipeline=None):
        self.db, self.memory, self.costs = db, memory, costs
        # The V3 pipeline, injected by the composition root. When it is
        # absent the legacy branches run, which is the pre-cut-over state.
        self.pipeline = pipeline
        from .learning_assets import LearningAssets
        self.assets = LearningAssets(self)

    def policy(self, owner_id="local-user", *, connection=None):
        if connection is None:
            with self.db.connection() as owned:
                return self.policy(owner_id, connection=owned)
        row = connection.execute("SELECT * FROM learning_policies WHERE owner_id=?", (owner_id,)).fetchone()
        if row is None:
            return {"owner_id": owner_id, "version": 0, "paused": True, "config": {}}
        return {**dict(row), "paused": bool(row["paused"]), "config": json.loads(row["config_json"])}

    def configure(self, owner_id, *, expected_version, paused, allowed_assets,
                  cycle_microusd=0, daily_microusd=0, monthly_microusd=0,
                  max_attempts=1, trial_days=7, prompt_suite_digest=None):
        if not owner_id or type(paused) is not bool or type(expected_version) is not int:
            raise ValueError("invalid learning authority")
        if not isinstance(allowed_assets, list) or any(item not in {"memory", "skill", "task_policy", "prompt"} for item in allowed_assets):
            raise ValueError("invalid allowed assets")
        if any(type(v) is not int or v < 0 for v in (cycle_microusd, daily_microusd, monthly_microusd)):
            raise ValueError("learning budgets must be nonnegative integers")
        if not cycle_microusd <= daily_microusd <= monthly_microusd:
            raise ValueError("cycle <= daily <= monthly budget is required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 500 or type(trial_days) is not int or not 1 <= trial_days <= 90:
            raise ValueError("invalid learning lifetime")
        if prompt_suite_digest is not None and (not isinstance(prompt_suite_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", prompt_suite_digest)):
            raise ValueError("invalid prompt suite digest")
        config = dict(allowed_assets=sorted(set(allowed_assets)), cycle_microusd=cycle_microusd,
                      daily_microusd=daily_microusd, monthly_microusd=monthly_microusd,
                      max_attempts=max_attempts, trial_days=trial_days, prompt_suite_digest=prompt_suite_digest)
        with self.db.transaction() as connection:
            current = self.policy(owner_id, connection=connection)
            if current["version"] != expected_version:
                raise LearningConflict("learning policy version changed")
            if expected_version == 0:
                connection.execute("INSERT INTO learning_policies(owner_id,version,paused,config_json,updated_at) VALUES (?,1,?,?,?)",
                                   (owner_id, int(paused), encode(config), now()))
            else:
                changed = connection.execute("UPDATE learning_policies SET version=version+1,paused=?,config_json=?,updated_at=? WHERE owner_id=? AND version=?",
                                             (int(paused), encode(config), now(), owner_id, expected_version)).rowcount
                if changed != 1:
                    raise LearningConflict("learning policy version changed")
        return self.policy(owner_id)

    def enqueue(self, owner_id, source_kind, source_id, source_hash, root_id, *, provenance="production", connection=None):
        if connection is None:
            with self.db.transaction() as owned:
                return self.enqueue(owner_id, source_kind, source_id, source_hash, root_id, provenance=provenance, connection=owned)
        if source_kind not in {"thread_message", "experience", "behavior_candidate", "goal_review", "method_feedback", "prompt_cycle", "research_result"} or provenance not in {"production", "acceptance"}:
            raise ValueError("invalid learning source")
        policy = self.policy(owner_id, connection=connection)
        job_id = "learning_" + digest([owner_id, source_kind, source_id, source_hash])
        timestamp = now()
        connection.execute(
            "INSERT INTO learning_jobs(id,owner_id,source_kind,source_id,source_hash,root_id,provenance,policy_version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,source_kind,source_id,source_hash) DO NOTHING",
            (job_id, owner_id, source_kind, source_id, source_hash, root_id, provenance, policy["version"], timestamp, timestamp))
        return job_id

    def collect(self, owner_id="local-user"):
        # ponytail: owner-local scan; replace with a transactional outbox when
        # message volume makes scanning material. Stable job keys prevent loss.
        with self.db.transaction() as connection:
            policy = self.policy(owner_id, connection=connection)
            if policy["paused"]:
                return 0
            rows = connection.execute(
                "SELECT m.*,t.project_id FROM thread_messages m JOIN threads t ON t.id=m.thread_id "
                "WHERE t.owner_id=? AND t.deleted_at IS NULL AND m.role='user' ORDER BY m.created_at,m.id", (owner_id,)).fetchall()
            for row in rows:
                if explicit_constraint(row["content"]) or (policy["config"].get("cycle_microusd", 0) > 0 and needs_constraint_extraction(row["content"])):
                    self.enqueue(owner_id, "thread_message", row["id"], digest(row["content"]), row["turn_id"] or row["id"], connection=connection)
            if self.pipeline is not None:
                # The V3 pipeline learns from Experiences and messages. The
                # scans below enqueue the legacy-only source kinds, whose apply
                # paths live in `learning_legacy`; enqueueing them would create
                # jobs the pipeline cannot read (V3 §63).
                return len(rows)
            if "prompt" in policy["config"].get("allowed_assets", []):
                if getattr(self, "prompt_learning", None) is not None:
                    self.prompt_learning.collect(owner_id, connection, policy)
                candidates = connection.execute("SELECT id,proposed_digest FROM evolution_candidates WHERE owner_id=? AND status='EVALUATED' AND candidate_type='prompt'", (owner_id,)).fetchall()
                for candidate in candidates:
                    self.enqueue(owner_id, "behavior_candidate", candidate["id"], candidate["proposed_digest"], candidate["id"], connection=connection)
            if "task_policy" in policy["config"].get("allowed_assets", []):
                research = connection.execute("SELECT j.id,j.topic FROM research_jobs j JOIN threads t ON t.id=j.thread_id WHERE t.owner_id=? AND t.deleted_at IS NULL AND j.status='COMPLETED'", (owner_id,)).fetchall()
                for item in research:
                    self.enqueue(owner_id, "research_result", item["id"], digest(item["topic"]), item["id"], connection=connection)
                reviews = connection.execute("SELECT r.id,r.source_hash,r.program_id FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id "
                                             "WHERE r.owner_id=? AND r.status='COMPLETED' AND r.evidence_stale=0 AND p.deleted_at IS NULL ORDER BY r.created_at,r.id", (owner_id,)).fetchall()
                for review in reviews:
                    self.enqueue(owner_id, "goal_review", review["id"], review["source_hash"], review["program_id"], connection=connection)
            if "skill" in policy["config"].get("allowed_assets", []):
                from .learning_workflow import METHOD
                feedback = connection.execute("SELECT f.id,f.note,a.program_id FROM goal_action_feedback f JOIN goal_actions a ON a.id=f.action_id "
                                              "JOIN goal_programs p ON p.id=a.program_id WHERE f.owner_id=? AND p.deleted_at IS NULL "
                                              "AND f.kind='method_success' AND f.note=? ORDER BY f.created_at,f.id", (owner_id, METHOD)).fetchall()
                for item in feedback:
                    self.enqueue(owner_id, "method_feedback", item["id"], digest(item["note"]), item["program_id"], connection=connection)
            return len(rows)

    def claim(self, owner_id="local-user", *, lease_seconds=60):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid learning lease")
        with self.db.transaction() as connection:
            policy = self.policy(owner_id, connection=connection)
            if policy["paused"]:
                return None
            timestamp = now()
            connection.execute("UPDATE learning_jobs SET status=CASE WHEN dispatched_at IS NULL THEN 'QUEUED' ELSE 'UNKNOWN' END,"
                               "lease_token=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE owner_id=? AND status='RUNNING' AND lease_until<=?",
                               (timestamp, owner_id, timestamp))
            suffix = " FOR UPDATE SKIP LOCKED" if self.db.backend == "postgresql" else ""
            row = connection.execute("SELECT * FROM learning_jobs WHERE owner_id=? AND status='QUEUED' ORDER BY created_at,id LIMIT 1" + suffix, (owner_id,)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
            changed = connection.execute("UPDATE learning_jobs SET status='RUNNING',lease_token=?,lease_until=?,policy_version=?,version=version+1,updated_at=? "
                                         "WHERE id=? AND status='QUEUED' AND version=?", (token, until, policy["version"], timestamp, row["id"], row["version"])).rowcount
            if changed != 1:
                return None
            return dict(connection.execute("SELECT * FROM learning_jobs WHERE id=?", (row["id"],)).fetchone())

    def _owned(self, connection, job):
        # Policy-row lock serializes adoption with a pause/configuration change.
        suffix = " FOR UPDATE" if self.db.backend == "postgresql" else ""
        connection.execute("SELECT owner_id FROM learning_policies WHERE owner_id=?" + suffix, (job["owner_id"],)).fetchone()
        policy = self.policy(job["owner_id"], connection=connection)
        current = connection.execute("SELECT * FROM learning_jobs WHERE id=? AND owner_id=?" + suffix, (job["id"], job["owner_id"])).fetchone()
        if current is None or current["status"] != "RUNNING" or current["lease_token"] != job["lease_token"] or current["lease_until"] <= now():
            raise LearningConflict("learning lease lost")
        if policy["paused"] or policy["version"] != current["policy_version"]:
            raise LearningConflict("learning authority paused or changed")
        return current, policy

    def dispatch(self, job):
        """Commit a unique independent budget and send marker BEFORE network I/O."""
        with self.db.transaction() as connection:
            current, policy = self._owned(connection, job)
            if current["dispatched_at"]:
                raise LearningConflict("remote learning request already dispatched")
            config = policy["config"]
            if config["cycle_microusd"] <= 0:
                raise LearningConflict("paid learning has no configured budget")
            for period, limit in ((self.costs.today_period(), config["daily_microusd"]), (self.costs.today_period()[:7], config["monthly_microusd"])):
                reserved = connection.execute(
                    "SELECT COALESCE(SUM(b.limit_microusd),0) FROM learning_jobs j JOIN cost_budgets b ON b.owner_id=j.owner_id "
                    "AND b.period_kind='ROOT' AND b.period_key=j.root_budget_id WHERE j.owner_id=? AND j.dispatched_at LIKE ?",
                    (job["owner_id"], period + "%"),).fetchone()[0]
                # Conservative reservation survives UNKNOWN and process crashes.
                if monetary_limits_enabled() and reserved + config["cycle_microusd"] > limit:
                    raise LearningConflict("learning aggregate budget exhausted")
            # Every learning request also requires owner-wide limits. Learning
            # does not create/reset those budgets at the start of each cycle.
            for kind, period in (("DAILY", self.costs.today_period()), ("MONTHLY", self.costs.today_period()[:7])):
                if monetary_limits_enabled() and not connection.execute("SELECT 1 FROM cost_budgets WHERE owner_id=? AND period_kind=? AND period_key=?",
                                          (job["owner_id"], kind, period)).fetchone():
                    raise LearningConflict("owner aggregate budget is not configured")
            root = self.costs.create_root_budget(job["owner_id"], "learning", job["id"], max_attempts=config["max_attempts"],
                                                deadline_at=current["lease_until"], limit_microusd=config["cycle_microusd"], connection=connection)
            connection.execute("UPDATE learning_jobs SET dispatched_at=?,root_budget_id=?,version=version+1,updated_at=? WHERE id=?", (now(), root["id"], now(), job["id"]))
            return root["id"]

    def assert_learning_call_allowed(self, owner_id, root_id):
        if root_id is None:
            return False
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM learning_jobs WHERE owner_id=? AND root_budget_id=?", (owner_id, root_id)).fetchone()
            if row is None:
                return False
            self._owned(connection, dict(row))
            return True

    def finish(self, job, status, *, changes=None, checkpoint=None, reason=""):
        """Close a claimed job under its own lease.

        The public counterpart of `_finish`: a worker outside this module needs a
        way to commit a result that still fails when the lease was lost (V3 §50).
        """
        with self.db.transaction() as connection:
            self._owned(connection, job)
            self._finish(connection, job, status, changes=changes, checkpoint=checkpoint, reason=reason)

    def _finish(self, connection, job, status, *, changes=None, checkpoint=None, reason=""):
        if status == "APPLIED":
            source_kind = {"method_feedback": "feedback", "behavior_candidate": "experience", "scoped_message": "thread_message"}.get(job["source_kind"], job["source_kind"])
            sources = [(source_kind, job["source_id"])] if source_kind in {"thread_message", "experience", "goal_review", "feedback"} else []
            sources.extend(("feedback", identity) for identity in (checkpoint or {}).get("feedback_ids", []))
            sources.extend(("experience", identity) for identity in (checkpoint or {}).get("experience_ids", []))
            self.assets.depend(job["id"], job["owner_id"], sources, connection=connection)
        changed = connection.execute("UPDATE learning_jobs SET status=?,change_set_json=?,checkpoint_json=?,reason=?,lease_token=NULL,lease_until=NULL,"
                                     "version=version+1,updated_at=? WHERE id=? AND status='RUNNING' AND lease_token=?",
                                     (status, encode(changes or []), encode(checkpoint or {}), reason, now(), job["id"], job["lease_token"])).rowcount
        if changed != 1:
            raise LearningConflict("learning lease lost")

    def run_once(self, owner_id="local-user"):
        self.maintain(owner_id)
        if self.pipeline is not None:
            self.pipeline.advance_canaries(owner_id)
        self.collect(owner_id)
        job = self.claim(owner_id, lease_seconds=3600)
        if job is None:
            return False
        self.execute(job)
        return True

    def execute(self, job):
        """Route a claimed job to the one learning pipeline (V3 §63).

        This module owns jobs, leases, budget, scheduling and recovery. What a
        job *learns* lives in `LearningPipeline`. Until the cut-over the legacy
        branches in `learning_legacy` stay reachable, so the frozen regression
        baseline keeps running while the new path is validated (V3 §65 step 23).
        """
        try:
            if self.pipeline is not None:
                self.pipeline.run_job(job)
            else:
                from .learning_legacy import execute_legacy
                execute_legacy(self, job)
        except Exception as exc:
            self.abandon(job, exc)

    def abandon(self, job, exc):
        """Close a failed cycle. A dispatched request is never replayed (V3 §48)."""
        with self.db.transaction() as connection:
            current = connection.execute(
                "SELECT dispatched_at,checkpoint_json,status,lease_token FROM learning_jobs WHERE id=?",
                (job["id"],)).fetchone()
            if current["status"] == "RUNNING" and current["lease_token"] == job["lease_token"]:
                self._finish(connection, job, "UNKNOWN" if current["dispatched_at"] else "REJECTED",
                             checkpoint=json.loads(current["checkpoint_json"]),
                             reason=type(exc).__name__ + ": " + str(exc)[:400])

    def resolve_research_policy(self, owner_id, project_id, fallback):
        if self.policy(owner_id)["paused"]:
            return fallback
        for job in self.history(owner_id):
            if job["status"] != "APPLIED":
                continue
            for change in job["changes"]:
                if change.get("purpose") != "research" or change.get("scope") not in (["user", ""], ["project", project_id]) or change["before"] != fallback:
                    continue
                with self.db.connection() as connection:
                    valid = all(connection.execute("SELECT 1 FROM research_jobs j JOIN threads t ON t.id=j.thread_id WHERE j.id=? AND t.owner_id=? AND t.deleted_at IS NULL AND j.status='COMPLETED'",
                                                   (identity, owner_id)).fetchone() for identity in job["checkpoint"].get("research_ids", []))
                if valid and (change["adoption"] == "ACTIVE" or change["expires_at"] > now()):
                    return change["after"]
        return fallback

    def maintain(self, owner_id="local-user"):
        """Monitor delivered-task evidence and expire unknown trials."""
        from .learning_workflow import METHOD
        for job in self.history(owner_id):
            if job["status"] != "APPLIED":
                continue
            for change in job["changes"]:
                kind = change.get("asset_type")
                if kind not in {"task_policy", "skill", "prompt"}:
                    continue
                if change.get("adoption") == "TRIAL" and change.get("expires_at", "") <= now():
                    self.suspend(job["id"], owner_id, expected_version=job["version"], reason="trial_expired_without_sufficient_evidence")
                    break
                if kind == "prompt":
                    continue
                results = {}
                with self.db.connection() as connection:
                    if kind == "task_policy" and change.get("purpose") == "research":
                        rows = connection.execute("SELECT j.id,j.status,e.data_json FROM research_jobs j JOIN threads t ON t.id=j.thread_id JOIN thread_events e ON e.turn_id=j.source_turn_id "
                                                  "WHERE t.owner_id=? AND e.type='task_policy.executed' AND j.status IN ('COMPLETED','PARTIAL','FAILED')", (owner_id,)).fetchall()
                        for row in rows:
                            if json.loads(row["data_json"]).get("bundle_id") == change["after"]:
                                results[row["id"]] = {"passed": row["status"] == "COMPLETED", "confounded": False}
                    elif kind == "task_policy":
                        rows = connection.execute("SELECT a.program_id,a.logical_key,a.estimated_minutes,f.actual_minutes,e.data_json "
                                                  "FROM goal_action_feedback f JOIN goal_actions a ON a.id=f.action_id JOIN goal_program_events e ON e.program_id=a.program_id "
                                                  "WHERE f.owner_id=? AND a.status='COMPLETED' AND f.actual_minutes>0 AND e.type='task_policy.executed' "
                                                  "ORDER BY f.created_at,f.id", (owner_id,)).fetchall()
                        for row in rows:
                            trace = json.loads(row["data_json"])
                            nominal = trace.get("nominal_minutes", {}).get(row["logical_key"])
                            if trace.get("bundle_id") != change["after"] or nominal is None:
                                continue
                            baseline_error = abs(nominal - row["actual_minutes"])
                            error = abs(row["estimated_minutes"] - row["actual_minutes"])
                            results[row["program_id"]] = {"passed": error <= baseline_error and results.get(row["program_id"], {}).get("passed", True), "baseline_error": baseline_error, "candidate_error": error,
                                                          "confounded": trace.get("confounded", False)}
                    else:
                        rows = connection.execute("SELECT f.kind,f.note,a.program_id,e.data_json FROM goal_action_feedback f JOIN goal_actions a ON a.id=f.action_id "
                                                  "JOIN goal_programs p ON p.id=a.program_id JOIN plan_document_versions v ON v.id=p.source_plan_document_version_id "
                                                  "JOIN thread_events e ON e.turn_id=v.source_turn_id WHERE f.owner_id=? AND a.status='COMPLETED' "
                                                  "AND e.type='skill.context_included' AND f.kind IN ('method_success','method_failure') ORDER BY f.created_at,f.id", (owner_id,)).fetchall()
                        for row in rows:
                            if change["after"] in json.loads(row["data_json"]).get("included_version_ids", []) and row["program_id"] not in job["checkpoint"].get("root_ids", []):
                                results[row["program_id"]] = {"passed": row["kind"] == "method_success" and row["note"] == METHOD, "confounded": False}
                if any(not result["passed"] for result in results.values()):
                    self.suspend(job["id"], owner_id, expected_version=job["version"], reason="observed_regression")
                    break
                if results:
                    checkpoint = {**job["checkpoint"], "task_results": results}
                    if len([result for result in results.values() if result["passed"] and not result["confounded"]]) >= 2:
                        change.update(adoption="ACTIVE", effect="SUPPORTED", evidence_scope="observed_task_outcomes; not_causal_or_statistical_certification")
                    with self.db.transaction() as connection:
                        connection.execute("UPDATE learning_jobs SET checkpoint_json=?,change_set_json=?,version=version+1,updated_at=? WHERE id=? AND owner_id=? AND version=? AND status='APPLIED'",
                                           (encode(checkpoint), encode(job["changes"]), now(), job["id"], owner_id, job["version"]))

    def matching_skills(self, owner_id, project_id, text):
        from .learning_workflow import METHOD, matches
        from .skill_platform import SkillPlatform
        if self.policy(owner_id)["paused"] or not matches(text):
            return []
        platform = SkillPlatform(self.db, self.skill_root, owner_id)
        selected = []
        for job in self.history(owner_id):
            if job["status"] != "APPLIED":
                continue
            for change in job["changes"]:
                if change.get("asset_type") != "skill" or change.get("scope") not in (["user", ""], ["project", project_id]) or (change["adoption"] == "TRIAL" and change["expires_at"] <= now()):
                    continue
                with self.db.connection() as connection:
                    valid = all(connection.execute("SELECT 1 FROM goal_action_feedback f JOIN goal_actions a ON a.id=f.action_id JOIN goal_programs p ON p.id=a.program_id "
                                                   "WHERE f.id=? AND f.owner_id=? AND f.note=? AND f.kind='method_success' AND p.deleted_at IS NULL",
                                                   (source_id, owner_id, METHOD)).fetchone() for source_id in job["checkpoint"]["feedback_ids"])
                version = platform.version(change["after"])
                if valid and version["status"] == "ENABLED":
                    selected.append(version)
        return selected

    def resolve_task_policy(self, owner_id, project_id, fallback_bundle_id):
        if self.policy(owner_id)["paused"]:
            return fallback_bundle_id
        for job in self.history(owner_id):
            if job["status"] != "APPLIED":
                continue
            for change in job["changes"]:
                if change.get("asset_type") != "task_policy" or change.get("purpose") != "compile_goal_program" or change.get("scope") not in (["user", ""], ["project", project_id]):
                    continue
                if change["before"] != fallback_bundle_id or (change["adoption"] == "TRIAL" and change["expires_at"] <= now()):
                    self.suspend(job["id"], owner_id, expected_version=job["version"], reason="baseline_changed_or_trial_expired")
                    continue
                with self.db.connection() as connection:
                    valid = connection.execute("SELECT 1 FROM goal_daily_reviews r JOIN goal_programs p ON p.id=r.program_id "
                                               "WHERE r.id=? AND r.source_hash=? AND r.status='COMPLETED' AND r.evidence_stale=0 AND p.owner_id=? AND p.deleted_at IS NULL",
                                               (job["source_id"], job["source_hash"], owner_id)).fetchone()
                    for feedback_id in job["checkpoint"].get("feedback_ids", []):
                        valid = valid and connection.execute("SELECT 1 FROM goal_action_feedback f JOIN goal_actions a ON a.id=f.action_id JOIN goal_programs p ON p.id=a.program_id "
                                                            "WHERE f.id=? AND f.owner_id=? AND p.deleted_at IS NULL AND NOT EXISTS "
                                                            "(SELECT 1 FROM goal_action_feedback newer WHERE newer.action_id=f.action_id AND (newer.created_at>f.created_at OR (newer.created_at=f.created_at AND newer.id>f.id)))",
                                                            (feedback_id, owner_id)).fetchone()
                if valid:
                    return change["after"]
                self.suspend(job["id"], owner_id, expected_version=job["version"], reason="source_invalidated")
        return fallback_bundle_id

    def resolve_prompt(self, owner_id, role, purpose, fallback_bundle_id):
        """Personal adoption never updates the global stable channel."""
        policy = self.policy(owner_id)
        if policy["paused"]:
            return fallback_bundle_id
        for job in self.history(owner_id):
            if job["status"] != "APPLIED":
                continue
            for change in job["changes"]:
                if change.get("asset_type") != "prompt" or change.get("role") != role or change.get("purpose") != purpose:
                    continue
                if change["adoption"] not in {"TRIAL", "ACTIVE"}:
                    continue
                if change["before"] != fallback_bundle_id or (change["adoption"] == "TRIAL" and change["expires_at"] <= now()):
                    self.suspend(job["id"], owner_id, expected_version=job["version"], reason="baseline_changed_or_trial_expired")
                    continue
                try:
                    self.evolution._validate_candidate_sources(self.evolution.get_candidate(change["candidate_id"], owner_id))
                except (ValueError, KeyError, RuntimeError):
                    self.suspend(job["id"], owner_id, expected_version=job["version"], reason="source_invalidated")
                    continue
                return change["after"]
        return fallback_bundle_id

    def assert_pinned_prompt_active(self, owner_id, bundle_id):
        """Revocation exception: reject a stale personalized prompt before send."""
        for job in self.history(owner_id):
            for change in job["changes"]:
                if change.get("asset_type") not in {"prompt", "task_policy"} or change["after"] != bundle_id:
                    continue
                if job["status"] != "APPLIED" or self.policy(owner_id)["paused"]:
                    raise LearningConflict("personalized prompt suspended")
                if change["adoption"] == "TRIAL" and change["expires_at"] <= now():
                    raise LearningConflict("personalized prompt trial expired")
                if change["asset_type"] == "prompt":
                    self.evolution._validate_candidate_sources(self.evolution.get_candidate(change["candidate_id"], owner_id))
                return

    def suspend(self, job_id, owner_id, *, expected_version, reason):
        with self.db.transaction() as connection:
            row = connection.execute("SELECT change_set_json FROM learning_jobs WHERE id=? AND owner_id=? AND version=? AND status='APPLIED'", (job_id, owner_id, expected_version)).fetchone()
            if row is None:
                raise LearningConflict("adoption version changed")
            changes = json.loads(row[0])
            for change in changes:
                change["adoption"] = "SUSPENDED"
                if reason == "observed_regression":
                    change["effect"] = "REFUTED"
                if change["asset_type"] == "memory":
                    entry = connection.execute("SELECT id,scope_type,scope_id FROM memory_entries WHERE owner_id=? AND current_revision_id=? AND status='ACTIVE'", (owner_id, change["after"])).fetchone()
                    if entry:
                        connection.execute("UPDATE memory_entries SET status='ARCHIVED',updated_at=? WHERE id=? AND current_revision_id=?", (now(), entry["id"], change["after"]))
                        connection.execute("DELETE FROM memory_fts WHERE entry_id=?", (entry["id"],))
                        self.memory._projection_intent(connection, owner_id, entry["scope_type"], entry["scope_id"], now())
                        pin_ids = connection.execute("SELECT pin_invocation_id FROM memory_context_pin_items WHERE source_type='revision' AND source_id=?", (change["after"],)).fetchall()
                        for pin in pin_ids:
                            connection.execute("UPDATE memory_context_pins SET invalidated_at=?,invalidation_reason='learning_suspended' WHERE model_invocation_id=?", (now(), pin[0]))
                            connection.execute("DELETE FROM memory_context_pin_payloads WHERE pin_invocation_id=?", (pin[0],))
                elif change["asset_type"] == "skill":
                    connection.execute("UPDATE skill_versions SET status='DISABLED' WHERE id=? AND skill_id IN (SELECT id FROM skills WHERE owner_id=?)", (change["after"], owner_id))
            changed = connection.execute("UPDATE learning_jobs SET status='SUSPENDED',reason=?,version=version+1,updated_at=? WHERE id=? AND owner_id=? AND version=? AND status='APPLIED'",
                                         (reason, now(), job_id, owner_id, expected_version)).rowcount
            if changed != 1:
                raise LearningConflict("adoption version changed")
            connection.execute("UPDATE learning_jobs SET change_set_json=? WHERE id=?", (encode(changes), job_id))
        self.memory.project(owner_id)
        self.assets.revoke(owner_id, "job", job_id, reason)

    def record_prompt_result(self, owner_id, bundle_id, task_id, *, success, safety_pass):
        for job in self.history(owner_id):
            if job["status"] != "APPLIED" or not any(change.get("asset_type") == "prompt" and change["after"] == bundle_id for change in job["changes"]):
                continue
            with self.db.transaction() as connection:
                # Count the actual writer invocation, not a bundle selection.
                hit = connection.execute("SELECT 1 FROM model_invocations WHERE owner_id=? AND run_id=? AND runtime_bundle_id=? "
                                         "AND role='researcher' AND purpose='write_research_section' AND system_prompt_digest<>'' LIMIT 1",
                                         (owner_id, task_id, bundle_id)).fetchone()
                if hit is None:
                    return
                candidate = self.evolution.get_candidate(job["changes"][0]["candidate_id"], owner_id)
                self.evolution._validate_candidate_sources(candidate)
                checkpoint = job["checkpoint"]
                results = checkpoint.setdefault("task_results", {})
                if task_id in results:
                    return
                results[task_id] = {"completed": success, "safety_pass": safety_pass}
                changes = job["changes"]
                status = "APPLIED"
                reason = ""
                if success is False or safety_pass is False:
                    status, reason = "SUSPENDED", "observed_regression"
                    for change in changes:
                        change.update(adoption="SUSPENDED", effect="REFUTED")
                elif sum(result["completed"] is True and result["safety_pass"] is True for result in results.values()) >= 2:
                    for change in changes:
                        change.update(adoption="ACTIVE", effect="SUPPORTED", evidence_scope="offline_gain_and_observed_online_safety; not_statistical_noninferiority")
                connection.execute("UPDATE learning_jobs SET checkpoint_json=?,change_set_json=?,status=?,reason=?,version=version+1,updated_at=? "
                                   "WHERE id=? AND owner_id=? AND version=? AND status='APPLIED'",
                                   (encode(checkpoint), encode(changes), status, reason, now(), job["id"], owner_id, job["version"]))
            return

    def history(self, owner_id="local-user"):
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM learning_jobs WHERE owner_id=? ORDER BY created_at DESC,id DESC", (owner_id,)).fetchall()
        return [{**dict(row), "changes": json.loads(row["change_set_json"]), "checkpoint": json.loads(row["checkpoint_json"])} for row in rows]

    def planning_constraints(self, owner_id, project_id, *, program_id=None, run_id=None, turn_id=None):
        """Only compile typed, still-valid explicit evidence; never execute lesson prose."""
        candidates = {}
        for job in self.history(owner_id):
            metadata = job["checkpoint"]
            if job["status"] != "APPLIED" or metadata.get("setting") != "action_max_minutes":
                continue
            scope = metadata["scope"]
            from .learning_assets import scope_matches
            applicability = metadata.get("applicability", {})
            if not scope_matches(applicability, program_id=program_id, run_id=run_id, turn_id=turn_id):
                continue
            key = (*scope, bool(applicability))
            if key in candidates or (scope != ["user", ""] and scope != ["project", project_id]):
                continue
            try:
                entry = self.memory.get(metadata["entry_id"], owner_id)
            except KeyError:
                continue
            if entry.status != "ACTIVE" or entry.revision_id != metadata["revision_id"] or entry.evidence_state != "VERIFIED":
                continue
            with self.db.connection() as connection:
                source = connection.execute("SELECT content FROM thread_messages WHERE id=?", (job["source_id"],)).fetchone()
                validity = connection.execute("SELECT valid_until FROM memory_entries WHERE id=?", (entry.id,)).fetchone()
            if source is None or digest(source["content"]) != job["source_hash"] or (validity["valid_until"] and validity["valid_until"] <= now()):
                continue
            candidates[key] = metadata
        return next((candidates[key] for key in (("project", project_id, True), ("user", "", True), ("project", project_id, False), ("user", "", False)) if key in candidates), None)

    def bind_constraint_scope(self, owner_id, message_id, applicability):
        if not isinstance(applicability, dict) or len(applicability) != 1 or set(applicability) - {"program_id", "run_id", "turn_id"}:
            raise ValueError("exactly one program/run/turn constraint scope is required")
        key, identity = next(iter(applicability.items()))
        if not isinstance(identity, str) or not identity:
            raise ValueError("invalid constraint identity")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT m.*,t.owner_id FROM thread_messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=? AND t.deleted_at IS NULL", (message_id,)).fetchone()
            if row is None or row["owner_id"] != owner_id or row["role"] != "user" or explicit_constraint(row["content"]) is None:
                raise LearningConflict("explicit constraint source is invalid")
            queries = {
                "program_id": "SELECT 1 FROM goal_programs WHERE id=? AND owner_id=? AND deleted_at IS NULL",
                "turn_id": "SELECT 1 FROM turns r JOIN threads t ON t.id=r.thread_id WHERE r.id=? AND t.owner_id=? AND t.deleted_at IS NULL",
                "run_id": "SELECT 1 FROM runs r JOIN turns u ON u.id=r.source_turn_id JOIN threads t ON t.id=u.thread_id WHERE r.id=? AND t.owner_id=? AND t.deleted_at IS NULL",
            }
            if connection.execute(queries[key], (identity, owner_id)).fetchone() is None:
                raise LearningConflict("constraint scope belongs to another owner or is missing")
            job_id = self.enqueue(owner_id, "thread_message", message_id, digest(row["content"]), row["turn_id"], connection=connection)
            changed = connection.execute("UPDATE learning_jobs SET checkpoint_json=?,status='QUEUED',version=version+1,updated_at=? WHERE id=? AND status IN ('QUEUED','NO_CHANGE') AND dispatched_at IS NULL",
                                         (encode({"applicability": applicability}), now(), job_id)).rowcount
            if changed != 1:
                raise LearningConflict("constraint job already processed")
            return job_id
