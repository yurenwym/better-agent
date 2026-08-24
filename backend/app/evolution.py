from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .behavior import BehaviorBundleService
from .db import Database


OWNER_ID = "local-user"
GATE_POLICY_VERSION = "deterministic-v1"


class EvolutionConflict(RuntimeError):
    pass


class EvolutionGateError(EvolutionConflict):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _json(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _future(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("expires_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    return parsed > datetime.now(timezone.utc)


class EvolutionService:
    def __init__(self, db: Database, bundles: BehaviorBundleService, minimum_canary_samples: int = 3) -> None:
        self.db = db
        self.bundles = bundles
        self.minimum_canary_samples = minimum_canary_samples

    def record_experience(
        self, *, task_type: str, outcome: str, lineage_group_hash: str, source_content_hash: str,
        runtime_bundle_id: str, dataset_partition: str, idempotency_key: str, owner_id: str = OWNER_ID,
    ) -> dict[str, Any]:
        partition = dataset_partition.upper()
        if partition not in {"DISCOVERY", "DEV", "HOLDOUT", "SAFETY"}:
            raise ValueError("invalid dataset_partition")
        payload = {
            "owner_id": owner_id, "task_type": task_type, "outcome": outcome,
            "lineage_group_hash": lineage_group_hash, "source_content_hash": source_content_hash,
            "runtime_bundle_id": runtime_bundle_id, "dataset_partition": partition,
        }
        request_digest = _digest(payload)
        now = _now()
        with self.db.transaction() as connection:
            cached = self._cached(connection, "evolution_experiences", idempotency_key, request_digest)
            if cached is not None:
                return self._experience(cached)
            if connection.execute("SELECT 1 FROM runtime_bundles WHERE id=?", (runtime_bundle_id,)).fetchone() is None:
                raise KeyError(runtime_bundle_id)
            prior = connection.execute(
                "SELECT dataset_partition FROM evolution_experiences WHERE owner_id=? AND lineage_group_hash=?",
                (owner_id, lineage_group_hash),
            ).fetchone()
            if prior is not None:
                raise EvolutionConflict("lineage partition is immutable")
            experience_id = f"experience_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO evolution_experiences(id,owner_id,task_type,outcome,lineage_group_hash,source_content_hash,"
                "runtime_bundle_id,dataset_partition,request_digest,idempotency_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (experience_id, owner_id, task_type, outcome, lineage_group_hash, source_content_hash,
                 runtime_bundle_id, partition, request_digest, idempotency_key, now),
            )
            self._event(connection, None, "evolution.experience.recorded", "observer", {"experience_id": experience_id}, f"experience:{idempotency_key}")
            return self._experience(connection.execute("SELECT * FROM evolution_experiences WHERE id=?", (experience_id,)).fetchone())

    def propose_candidate(
        self, *, candidate_type: str, experience_ids: list[str], base_bundle_id: str, target_bundle_id: str,
        proposed_content: dict[str, Any], permission_diff: dict[str, Any], reason: str,
        idempotency_key: str, owner_id: str = OWNER_ID,
    ) -> dict[str, Any]:
        if candidate_type not in {"memory", "skill", "policy", "prompt", "code"}:
            raise ValueError("invalid candidate_type")
        if not isinstance(experience_ids, list):
            raise ValueError("experience_ids must be a list")
        if not isinstance(proposed_content, dict) or not isinstance(permission_diff, dict):
            raise ValueError("proposed_content and permission_diff must be objects")
        frozen_keys = {"core_policy", "gate_policy", "promotion_policy", "permissions"}
        if frozen_keys.intersection(proposed_content):
            raise EvolutionGateError("candidate cannot modify frozen core or release policy")
        if permission_diff.get("added"):
            raise EvolutionGateError("candidate cannot expand permissions")
        proposed_json, permission_json = _json(proposed_content), _json(permission_diff)
        payload = {
            "owner_id": owner_id, "candidate_type": candidate_type, "experience_ids": sorted(experience_ids),
            "base_bundle_id": base_bundle_id, "target_bundle_id": target_bundle_id,
            "proposed_content": proposed_content, "permission_diff": permission_diff, "reason": reason,
        }
        request_digest = _digest(payload)
        with self.db.transaction() as connection:
            cached = self._cached(connection, "evolution_candidates", idempotency_key, request_digest)
            if cached is not None:
                return self._candidate(cached)
            base = self._bundle_row(connection, base_bundle_id)
            target = self._bundle_row(connection, target_bundle_id)
            base_manifest, target_manifest = json.loads(base["manifest_json"]), json.loads(target["manifest_json"])
            actual_diff = {key: value for key, value in target_manifest.items() if base_manifest.get(key) != value}
            if proposed_content != actual_diff:
                raise EvolutionGateError("proposed content must match the target bundle diff")
            if base_manifest.get("core_policy") != target_manifest.get("core_policy"):
                raise EvolutionGateError("candidate cannot modify core policy")
            base_permissions = set(base_manifest.get("permissions", []))
            if not set(target_manifest.get("permissions", [])).issubset(base_permissions):
                raise EvolutionGateError("target bundle cannot expand permissions")
            unique_ids = list(dict.fromkeys(experience_ids))
            rows = []
            if unique_ids:
                placeholders = ",".join("?" for _ in unique_ids)
                rows = connection.execute(
                    f"SELECT * FROM evolution_experiences WHERE owner_id=? AND id IN ({placeholders})",
                    (owner_id, *unique_ids),
                ).fetchall()
            lineages = {row["lineage_group_hash"] for row in rows if row["dataset_partition"] == "DISCOVERY"}
            if len(rows) != len(unique_ids) or len(lineages) < 3:
                raise EvolutionGateError("at least three independent DISCOVERY experiences are required")
            candidate_id = f"candidate_{uuid.uuid4().hex}"
            now = _now()
            connection.execute(
                "INSERT INTO evolution_candidates(id,owner_id,candidate_type,experience_ids_json,base_bundle_id,target_bundle_id,"
                "target_bundle_digest,proposed_content_json,proposed_digest,permission_diff_json,permission_diff_digest,reason,status,"
                "version,request_digest,idempotency_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'READY_FOR_EVAL',0,?,?,?,?)",
                (candidate_id, owner_id, candidate_type, _json(unique_ids), base_bundle_id, target_bundle_id,
                 target["bundle_hash"], proposed_json, _digest(proposed_json), permission_json, _digest(permission_json), reason,
                 request_digest, idempotency_key, now, now),
            )
            self._event(connection, candidate_id, "evolution.candidate.created", "proposer", {"experience_count": len(unique_ids)}, f"candidate:{idempotency_key}")
            return self._candidate_row(connection, candidate_id, owner_id)

    def evaluate(
        self, candidate_id: str, *, expected_version: int, deterministic_checks: dict[str, bool], metrics: dict[str, Any],
        eval_set_digest: str, evaluator_digest: str, idempotency_key: str, owner_id: str = OWNER_ID,
    ) -> dict[str, Any]:
        if not isinstance(deterministic_checks, dict) or not deterministic_checks or not all(isinstance(value, bool) for value in deterministic_checks.values()):
            raise ValueError("deterministic_checks must be a non-empty boolean object")
        if not isinstance(metrics, dict):
            raise ValueError("metrics must be an object")
        payload = {"candidate_id": candidate_id, "expected_version": expected_version, "checks": deterministic_checks,
                   "metrics": metrics, "eval_set_digest": eval_set_digest, "evaluator_digest": evaluator_digest}
        request_digest = _digest(payload)
        with self.db.transaction() as connection:
            cached = self._cached(connection, "evolution_evaluations", idempotency_key, request_digest)
            if cached is not None:
                return self._evaluation(cached)
            candidate = self._candidate_db(connection, candidate_id, owner_id)
            if candidate["status"] != "READY_FOR_EVAL" or candidate["version"] != expected_version:
                raise EvolutionConflict("candidate is not ready for evaluation")
            deterministic_pass = bool(deterministic_checks) and all(value is True for value in deterministic_checks.values())
            report = {
                "candidate_digest": candidate["proposed_digest"], "target_bundle_digest": candidate["target_bundle_digest"],
                "eval_set_digest": eval_set_digest, "evaluator_digest": evaluator_digest,
                "deterministic_pass": deterministic_pass, "checks": deterministic_checks, "metrics": metrics,
            }
            evaluation_id = f"evaluation_{uuid.uuid4().hex}"
            now = _now()
            report_digest = _digest(report)
            connection.execute(
                "INSERT INTO evolution_evaluations(id,candidate_id,baseline_bundle_id,candidate_bundle_id,eval_set_digest,evaluator_digest,"
                "deterministic_pass,checks_json,metrics_json,report_digest,status,request_digest,idempotency_key,created_at,finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'COMPLETED',?,?,?,?)",
                (evaluation_id, candidate_id, candidate["base_bundle_id"], candidate["target_bundle_id"], eval_set_digest,
                 evaluator_digest, int(deterministic_pass), _json(deterministic_checks), _json(metrics), report_digest,
                 request_digest, idempotency_key, now, now),
            )
            self._advance(connection, candidate_id, expected_version, "EVALUATED", current_evaluation_id=evaluation_id)
            self._event(connection, candidate_id, "evolution.evaluation.completed", "deterministic-evaluator",
                        {"evaluation_id": evaluation_id, "deterministic_pass": deterministic_pass}, f"evaluation:{idempotency_key}")
            return self._evaluation(connection.execute("SELECT * FROM evolution_evaluations WHERE id=?", (evaluation_id,)).fetchone())

    def evaluate_builtin(self, candidate_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        """Run the server-owned deterministic suite; clients cannot self-report gates."""
        from .evals import run_deterministic_suite

        report = run_deterministic_suite()
        checks = {item.name: item.passed for item in report.results}
        item = self.get_candidate(candidate_id, owner_id)
        base = self.bundles.get(item["base_bundle_id"]).manifest
        target = self.bundles.get(item["target_bundle_id"]).manifest
        checks["candidate_diff_bound"] = item["proposed_content"] == {
            key: value for key, value in target.items() if base.get(key) != value
        }
        checks["target_bundle_digest"] = self.bundles.get(item["target_bundle_id"]).bundle_hash == item["target_bundle_digest"]
        metrics = {"passed": report.passed, "total": len(report.results)}
        return self.evaluate(
            candidate_id, expected_version=expected_version, deterministic_checks=checks, metrics=metrics,
            eval_set_digest=_digest([item.name for item in report.results]), evaluator_digest="builtin-deterministic-v1",
            idempotency_key=idempotency_key, owner_id=owner_id,
        )

    def approve_current(self, candidate_id: str, *, expires_at: str, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        item = self.get_candidate(candidate_id, owner_id)
        evaluation = self.get_evaluation(item["current_evaluation_id"])
        return self.approve(
            candidate_id, expected_version=item["version"], evaluation_id=evaluation["id"],
            candidate_digest=item["proposed_digest"], evaluation_report_digest=evaluation["report_digest"],
            permission_diff_digest=item["permission_diff_digest"], target_bundle_digest=item["target_bundle_digest"],
            expires_at=expires_at, actor="user", idempotency_key=idempotency_key, owner_id=owner_id,
        )

    def approve_builtin(self, candidate_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        item = self.get_candidate(candidate_id, owner_id)
        if item["version"] != expected_version: raise EvolutionConflict("candidate version conflict")
        return self.approve_current(candidate_id, expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), idempotency_key=idempotency_key, owner_id=owner_id)

    def start_canary_builtin(self, candidate_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        item = self.get_candidate(candidate_id, owner_id)
        if item["version"] != expected_version or not item.get("approval_id"): raise EvolutionConflict("candidate is not approved for canary")
        return self.start_canary(candidate_id, expected_version=expected_version, approval_id=item["approval_id"], allocation_percent=10, assignment_unit="run", idempotency_key=idempotency_key, owner_id=owner_id)

    def approve(
        self, candidate_id: str, *, expected_version: int, evaluation_id: str, candidate_digest: str,
        evaluation_report_digest: str, permission_diff_digest: str, target_bundle_digest: str,
        expires_at: str, actor: str, idempotency_key: str, owner_id: str = OWNER_ID,
    ) -> dict[str, Any]:
        if not _future(expires_at):
            raise EvolutionGateError("approval expiry must be in the future")
        payload = {"candidate_id": candidate_id, "expected_version": expected_version, "evaluation_id": evaluation_id,
                   "candidate_digest": candidate_digest, "evaluation_report_digest": evaluation_report_digest,
                   "permission_diff_digest": permission_diff_digest, "target_bundle_digest": target_bundle_digest,
                   "expires_at": expires_at, "actor": actor}
        request_digest = _digest(payload)
        with self.db.transaction() as connection:
            cached = self._cached(connection, "evolution_decisions", idempotency_key, request_digest)
            if cached is not None:
                return self._decision(cached)
            candidate = self._candidate_db(connection, candidate_id, owner_id)
            evaluation = connection.execute("SELECT * FROM evolution_evaluations WHERE id=? AND candidate_id=?", (evaluation_id, candidate_id)).fetchone()
            if candidate["status"] != "EVALUATED" or candidate["version"] != expected_version or evaluation is None:
                raise EvolutionConflict("candidate is not awaiting approval")
            if not evaluation["deterministic_pass"]:
                raise EvolutionGateError("deterministic evaluation failed")
            binding = (candidate["proposed_digest"], evaluation["report_digest"], candidate["permission_diff_digest"], candidate["target_bundle_digest"])
            if binding != (candidate_digest, evaluation_report_digest, permission_diff_digest, target_bundle_digest):
                raise EvolutionConflict("approval binding does not match frozen candidate")
            decision_id = f"decision_{uuid.uuid4().hex}"
            now = _now()
            connection.execute(
                "INSERT INTO evolution_decisions(id,candidate_id,evaluation_id,decision,from_bundle_id,to_bundle_id,actor,reason,"
                "candidate_digest,evaluation_report_digest,permission_diff_digest,target_bundle_digest,expires_at,request_digest,idempotency_key,created_at) "
                "VALUES (?,?,?,'APPROVE',?,?,?,'',?,?,?,?,?,?,?,?)",
                (decision_id, candidate_id, evaluation_id, candidate["base_bundle_id"], candidate["target_bundle_id"], actor,
                 *binding, expires_at, request_digest, idempotency_key, now),
            )
            self._advance(connection, candidate_id, expected_version, "APPROVED", approval_id=decision_id)
            self._event(connection, candidate_id, "evolution.approval.decided", actor, {"decision": "APPROVE", "decision_id": decision_id}, f"approval:{idempotency_key}")
            return self._decision(connection.execute("SELECT * FROM evolution_decisions WHERE id=?", (decision_id,)).fetchone())

    def reject(self, candidate_id: str, *, expected_version: int, reason: str, actor: str,
               idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_digest = _digest({"candidate_id": candidate_id, "expected_version": expected_version, "reason": reason, "actor": actor})
        with self.db.transaction() as connection:
            cached = self._cached(connection, "evolution_decisions", idempotency_key, request_digest)
            if cached is not None:
                return self._decision(cached)
            candidate = self._candidate_db(connection, candidate_id, owner_id)
            if candidate["version"] != expected_version or candidate["status"] not in {"READY_FOR_EVAL", "EVALUATED", "APPROVED"}:
                raise EvolutionConflict("candidate cannot be rejected")
            evaluation = connection.execute("SELECT * FROM evolution_evaluations WHERE id=?", (candidate["current_evaluation_id"],)).fetchone()
            decision_id = f"decision_{uuid.uuid4().hex}"
            now = _now()
            connection.execute(
                "INSERT INTO evolution_decisions(id,candidate_id,evaluation_id,decision,from_bundle_id,to_bundle_id,actor,reason,"
                "candidate_digest,evaluation_report_digest,permission_diff_digest,target_bundle_digest,request_digest,idempotency_key,created_at) "
                "VALUES (?,?,?,'REJECT',?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, candidate_id, candidate["current_evaluation_id"], candidate["base_bundle_id"], candidate["target_bundle_id"], actor, reason,
                 candidate["proposed_digest"], evaluation["report_digest"] if evaluation else "", candidate["permission_diff_digest"],
                 candidate["target_bundle_digest"], request_digest, idempotency_key, now),
            )
            self._advance(connection, candidate_id, expected_version, "REJECTED")
            self._event(connection, candidate_id, "evolution.approval.decided", actor, {"decision": "REJECT"}, f"reject:{idempotency_key}")
            return self._decision(connection.execute("SELECT * FROM evolution_decisions WHERE id=?", (decision_id,)).fetchone())

    def start_canary(
        self, candidate_id: str, *, expected_version: int, approval_id: str, allocation_percent: int,
        assignment_unit: str, idempotency_key: str, owner_id: str = OWNER_ID,
    ) -> dict[str, Any]:
        if isinstance(allocation_percent, bool) or not 1 <= allocation_percent <= 100:
            raise ValueError("allocation_percent must be between 1 and 100")
        payload = {"candidate_id": candidate_id, "expected_version": expected_version, "approval_id": approval_id,
                   "allocation_percent": allocation_percent, "assignment_unit": assignment_unit}
        request_digest = _digest(payload)
        with self.db.transaction() as connection:
            cached = self._cached(connection, "canary_deployments", idempotency_key, request_digest)
            if cached is not None:
                return self._deployment(cached)
            if connection.execute("SELECT 1 FROM canary_deployments WHERE status='ACTIVE'").fetchone() is not None:
                raise EvolutionConflict("another canary deployment is already active")
            candidate = self._candidate_db(connection, candidate_id, owner_id)
            approval = connection.execute(
                "SELECT * FROM evolution_decisions WHERE id=? AND candidate_id=? AND decision='APPROVE'", (approval_id, candidate_id)
            ).fetchone()
            if candidate["status"] != "APPROVED" or candidate["version"] != expected_version or approval is None:
                raise EvolutionConflict("candidate is not approved for canary")
            self._validate_approval(candidate, approval)
            deployment_id = f"canary_{uuid.uuid4().hex}"
            salt_digest = _digest({"candidate_id": candidate_id, "deployment_id": deployment_id})
            now = _now()
            connection.execute(
                "INSERT INTO canary_deployments(id,candidate_id,approval_id,champion_bundle_id,challenger_bundle_id,allocation_percent,"
                "assignment_unit,salt_digest,gate_policy_version,status,request_digest,idempotency_key,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?,?)",
                (deployment_id, candidate_id, approval_id, candidate["base_bundle_id"], candidate["target_bundle_id"],
                 allocation_percent, assignment_unit, salt_digest, GATE_POLICY_VERSION, request_digest, idempotency_key, now, now),
            )
            self._switch_channel(connection, "canary", candidate["target_bundle_id"], f"evolution-canary:{idempotency_key}")
            self._advance(connection, candidate_id, expected_version, "CANARY", deployment_id=deployment_id)
            self._event(connection, candidate_id, "evolution.canary.started", "release-manager", {"deployment_id": deployment_id}, f"canary:{idempotency_key}")
            return self._deployment(connection.execute("SELECT * FROM canary_deployments WHERE id=?", (deployment_id,)).fetchone())

    def record_exposure(
        self, deployment_id: str, run_id: str, assignment_key: str, *, success: bool, safety_pass: bool,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or f"{deployment_id}:{run_id}"
        payload = {"deployment_id": deployment_id, "run_id": run_id, "assignment_key": assignment_key,
                   "success": success, "safety_pass": safety_pass}
        request_digest = _digest(payload)
        with self.db.transaction() as connection:
            cached = self._cached(connection, "canary_exposures", key, request_digest)
            if cached is not None:
                return self._exposure(cached)
            prior = connection.execute(
                "SELECT * FROM canary_exposures WHERE deployment_id=? AND run_id=?", (deployment_id, run_id)
            ).fetchone()
            if prior is not None:
                raise EvolutionConflict("run exposure is already frozen")
            deployment = connection.execute("SELECT * FROM canary_deployments WHERE id=?", (deployment_id,)).fetchone()
            if deployment is None:
                raise KeyError(deployment_id)
            if deployment["status"] != "ACTIVE":
                raise EvolutionConflict("canary is not active")
            assignment_hash = _digest(f"{deployment['salt_digest']}:{assignment_key}")
            challenger = int(assignment_hash[:16], 16) % 100 < deployment["allocation_percent"]
            cohort = "challenger" if challenger else "champion"
            bundle_id = deployment["challenger_bundle_id"] if challenger else deployment["champion_bundle_id"]
            now = _now()
            connection.execute(
                "INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,request_digest,idempotency_key,exposed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (deployment_id, run_id, assignment_hash, cohort, bundle_id, int(success), int(safety_pass), request_digest, key, now),
            )
            return self._exposure(connection.execute("SELECT * FROM canary_exposures WHERE deployment_id=? AND run_id=?", (deployment_id, run_id)).fetchone())

    def assign_run(self, run_id: str, assignment_key: str, *, connection=None) -> tuple[str, str | None]:
        """Pin a new run to stable or to the currently active deterministic Canary cohort."""
        if connection is None:
            with self.db.transaction() as owned:
                return self._assign_run(owned, run_id, assignment_key)
        return self._assign_run(connection, run_id, assignment_key)

    def _assign_run(self, connection, run_id: str, assignment_key: str) -> tuple[str, str | None]:
        existing = connection.execute(
            "SELECT bundle_id,deployment_id FROM canary_exposures WHERE run_id=? ORDER BY exposed_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if existing is not None:
            return existing["bundle_id"], existing["deployment_id"]
        deployment = connection.execute(
            "SELECT * FROM canary_deployments WHERE status='ACTIVE' ORDER BY created_at DESC,id DESC LIMIT 1"
        ).fetchone()
        if deployment is None:
            stable = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
            if stable is None:
                raise KeyError("stable")
            return stable["bundle_id"], None
        subject = run_id if deployment["assignment_unit"] == "run" else assignment_key
        assignment_hash = _digest(f"{deployment['salt_digest']}:{subject}")
        challenger = int(assignment_hash[:16], 16) % 100 < deployment["allocation_percent"]
        cohort = "challenger" if challenger else "champion"
        bundle_id = deployment["challenger_bundle_id"] if challenger else deployment["champion_bundle_id"]
        request_digest = _digest({
            "deployment_id": deployment["id"], "run_id": run_id,
            "assignment_key": assignment_key, "cohort": cohort,
        })
        connection.execute(
            "INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,request_digest,idempotency_key,exposed_at) "
            "VALUES (?,?,?,?,?,0,1,?,?,?)",
            (deployment["id"], run_id, assignment_hash, cohort, bundle_id, request_digest,
             f"auto-exposure:{run_id}", _now()),
        )
        return bundle_id, deployment["id"]

    def finish_run_exposure(
        self, run_id: str, *, success: bool, safety_pass: bool = True, connection=None,
    ) -> None:
        if connection is None:
            with self.db.transaction() as owned:
                self._finish_run_exposure(owned, run_id, success, safety_pass)
            return
        self._finish_run_exposure(connection, run_id, success, safety_pass)

    def _finish_run_exposure(self, connection, run_id: str, success: bool, safety_pass: bool) -> None:
        exposure = connection.execute(
            "SELECT e.*,d.candidate_id,d.champion_bundle_id,d.status deployment_status "
            "FROM canary_exposures e JOIN canary_deployments d ON d.id=e.deployment_id WHERE e.run_id=?",
            (run_id,),
        ).fetchone()
        if exposure is None:
            return
        connection.execute(
            "UPDATE canary_exposures SET success=?,safety_pass=? WHERE deployment_id=? AND run_id=?",
            (int(success), int(safety_pass), exposure["deployment_id"], run_id),
        )
        if safety_pass or exposure["cohort"] != "challenger" or exposure["deployment_status"] != "ACTIVE":
            return
        now = _now()
        connection.execute(
            "UPDATE canary_deployments SET status='ROLLED_BACK',updated_at=? WHERE id=? AND status='ACTIVE'",
            (now, exposure["deployment_id"]),
        )
        candidate = connection.execute(
            "SELECT version,status FROM evolution_candidates WHERE id=?", (exposure["candidate_id"],)
        ).fetchone()
        if candidate and candidate["status"] == "CANARY":
            self._advance(connection, exposure["candidate_id"], candidate["version"], "ROLLED_BACK")
        self._switch_channel(
            connection, "canary", exposure["champion_bundle_id"],
            f"auto-safety-rollback-channel:{exposure['deployment_id']}",
        )
        self._event(
            connection, exposure["candidate_id"], "evolution.canary.auto_rolled_back", "safety-gate",
            {"deployment_id": exposure["deployment_id"], "run_id": run_id},
            f"auto-safety-rollback:{exposure['deployment_id']}",
        )

    def promote(self, candidate_id: str, *, expected_version: int, idempotency_key: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        request_digest = _digest({"candidate_id": candidate_id, "expected_version": expected_version})
        with self.db.transaction() as connection:
            prior = connection.execute("SELECT * FROM evolution_decisions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["request_digest"] != request_digest:
                    raise EvolutionConflict("idempotency key payload changed")
                return self._candidate_row(connection, candidate_id, owner_id)
            candidate = self._candidate_db(connection, candidate_id, owner_id)
            if candidate["status"] != "CANARY" or candidate["version"] != expected_version:
                raise EvolutionConflict("candidate is not in canary")
            approval = connection.execute("SELECT * FROM evolution_decisions WHERE id=?", (candidate["approval_id"],)).fetchone()
            self._validate_approval(candidate, approval)
            deployment = connection.execute("SELECT * FROM canary_deployments WHERE id=? AND status='ACTIVE'", (candidate["deployment_id"],)).fetchone()
            exposures = connection.execute(
                "SELECT success,safety_pass FROM canary_exposures WHERE deployment_id=? AND cohort='challenger'",
                (candidate["deployment_id"],),
            ).fetchall()
            if len(exposures) < self.minimum_canary_samples:
                raise EvolutionGateError(f"at least {self.minimum_canary_samples} actual canary samples are required")
            if any(not row["safety_pass"] for row in exposures):
                raise EvolutionGateError("canary safety gate failed")
            if any(not row["success"] for row in exposures):
                raise EvolutionGateError("canary success gate failed")
            now = _now()
            decision_id = f"decision_{uuid.uuid4().hex}"
            binding = (candidate["proposed_digest"], approval["evaluation_report_digest"], candidate["permission_diff_digest"], candidate["target_bundle_digest"])
            connection.execute(
                "INSERT INTO evolution_decisions(id,candidate_id,evaluation_id,decision,from_bundle_id,to_bundle_id,actor,reason,"
                "candidate_digest,evaluation_report_digest,permission_diff_digest,target_bundle_digest,request_digest,idempotency_key,created_at) "
                "VALUES (?,?,?,'PROMOTE',?,?,'release-manager','',?,?,?,?,?,?,?)",
                (decision_id, candidate_id, candidate["current_evaluation_id"], candidate["base_bundle_id"], candidate["target_bundle_id"],
                 *binding, request_digest, idempotency_key, now),
            )
            self._switch_channel(connection, "stable", candidate["target_bundle_id"], f"evolution-promote:{idempotency_key}")
            connection.execute("UPDATE canary_deployments SET status='PROMOTED',updated_at=? WHERE id=? AND status='ACTIVE'", (now, deployment["id"]))
            self._advance(connection, candidate_id, expected_version, "PROMOTED")
            self._event(connection, candidate_id, "evolution.promoted", "release-manager", {"deployment_id": deployment["id"]}, f"promote:{idempotency_key}")
            return self._candidate_row(connection, candidate_id, owner_id)

    def rollback(
        self, candidate_id: str, *, expected_version: int, reason: str, idempotency_key: str, owner_id: str = OWNER_ID,
    ) -> dict[str, Any]:
        request_digest = _digest({"candidate_id": candidate_id, "expected_version": expected_version, "reason": reason})
        with self.db.transaction() as connection:
            prior = connection.execute("SELECT * FROM evolution_decisions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if prior["request_digest"] != request_digest:
                    raise EvolutionConflict("idempotency key payload changed")
                return self._candidate_row(connection, candidate_id, owner_id)
            candidate = self._candidate_db(connection, candidate_id, owner_id)
            if candidate["status"] not in {"CANARY", "PROMOTED"} or candidate["version"] != expected_version:
                raise EvolutionConflict("candidate cannot be rolled back")
            stable = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name='stable'").fetchone()
            expected_stable = candidate["target_bundle_id"] if candidate["status"] == "PROMOTED" else candidate["base_bundle_id"]
            if stable is None or stable["bundle_id"] != expected_stable:
                raise EvolutionConflict("stable channel changed after this candidate was promoted")
            approval = connection.execute("SELECT * FROM evolution_decisions WHERE id=?", (candidate["approval_id"],)).fetchone()
            decision_id = f"decision_{uuid.uuid4().hex}"
            now = _now()
            connection.execute(
                "INSERT INTO evolution_decisions(id,candidate_id,evaluation_id,decision,from_bundle_id,to_bundle_id,actor,reason,"
                "candidate_digest,evaluation_report_digest,permission_diff_digest,target_bundle_digest,request_digest,idempotency_key,created_at) "
                "VALUES (?,?,?,'ROLLBACK',?,?,'release-manager',?,?,?,?,?,?,?,?)",
                (decision_id, candidate_id, candidate["current_evaluation_id"], candidate["target_bundle_id"], candidate["base_bundle_id"], reason,
                 candidate["proposed_digest"], approval["evaluation_report_digest"], candidate["permission_diff_digest"],
                 candidate["target_bundle_digest"], request_digest, idempotency_key, now),
            )
            self._switch_channel(connection, "stable", candidate["base_bundle_id"], f"evolution-rollback:{idempotency_key}")
            if candidate["deployment_id"]:
                connection.execute("UPDATE canary_deployments SET status='ROLLED_BACK',updated_at=? WHERE id=?", (now, candidate["deployment_id"]))
            self._advance(connection, candidate_id, expected_version, "ROLLED_BACK")
            self._event(connection, candidate_id, "evolution.rolled_back", "release-manager", {"reason": reason}, f"rollback:{idempotency_key}")
            return self._candidate_row(connection, candidate_id, owner_id)

    def get_candidate(self, candidate_id: str, owner_id: str = OWNER_ID) -> dict[str, Any]:
        with self.db.connection() as connection:
            return self._candidate_row(connection, candidate_id, owner_id)

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM evolution_evaluations WHERE id=?", (evaluation_id,)).fetchone()
        if row is None:
            raise KeyError(evaluation_id)
        return self._evaluation(row)

    def list_candidates(self, owner_id: str = OWNER_ID) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM evolution_candidates WHERE owner_id=? ORDER BY created_at,id", (owner_id,)).fetchall()
            return [self._candidate_row(connection, row["id"], owner_id) for row in rows]

    def list_bundles(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT b.*,GROUP_CONCAT(c.name) channels FROM runtime_bundles b LEFT JOIN runtime_channels c ON c.bundle_id=b.id GROUP BY b.id ORDER BY b.created_at,b.id"
            ).fetchall()
        return [{"id": row["id"], "bundle_hash": row["bundle_hash"], "manifest": json.loads(row["manifest_json"]),
                 "channels": sorted(filter(None, (row["channels"] or "").split(","))), "created_at": row["created_at"]} for row in rows]

    def history(self, after_id: int = 0) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM evolution_events WHERE row_id>? ORDER BY row_id", (after_id,)).fetchall()
        return [{"row_id": row["row_id"], "event_id": row["event_id"], "candidate_id": row["candidate_id"],
                 "type": row["type"], "actor": row["actor"], "data": json.loads(row["data_json"]),
                 "occurred_at": row["occurred_at"]} for row in rows]

    def _validate_approval(self, candidate: Any, approval: Any) -> None:
        if approval is None or not approval["expires_at"] or not _future(approval["expires_at"]):
            raise EvolutionGateError("approval is expired")
        evaluation = self.get_evaluation(approval["evaluation_id"])
        binding = (candidate["proposed_digest"], evaluation["report_digest"], candidate["permission_diff_digest"], candidate["target_bundle_digest"])
        approved = (approval["candidate_digest"], approval["evaluation_report_digest"], approval["permission_diff_digest"], approval["target_bundle_digest"])
        if binding != approved:
            raise EvolutionConflict("approval binding is stale")

    @staticmethod
    def _cached(connection, table: str, key: str, request_digest: str):
        row = connection.execute(f"SELECT * FROM {table} WHERE idempotency_key=?", (key,)).fetchone()
        if row is not None and row["request_digest"] != request_digest:
            raise EvolutionConflict("idempotency key payload changed")
        return row

    @staticmethod
    def _bundle_row(connection, bundle_id: str):
        row = connection.execute("SELECT * FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone()
        if row is None:
            raise KeyError(bundle_id)
        return row

    @staticmethod
    def _advance(connection, candidate_id: str, expected_version: int, status: str, **fields: Any) -> None:
        assignments = ["status=?", "version=version+1", "updated_at=?", *[f"{name}=?" for name in fields]]
        values = [status, _now(), *fields.values(), candidate_id, expected_version]
        result = connection.execute(
            f"UPDATE evolution_candidates SET {','.join(assignments)} WHERE id=? AND version=?", values
        )
        if result.rowcount != 1:
            raise EvolutionConflict("candidate version changed")

    @staticmethod
    def _switch_channel(connection, channel: str, bundle_id: str, idempotency_key: str) -> None:
        current = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name=?", (channel,)).fetchone()
        now = _now()
        connection.execute(
            "INSERT INTO runtime_channels(name,bundle_id,version,updated_at) VALUES (?,?,0,?) "
            "ON CONFLICT(name) DO UPDATE SET bundle_id=excluded.bundle_id,version=runtime_channels.version+1,updated_at=excluded.updated_at",
            (channel, bundle_id, now),
        )
        connection.execute(
            "INSERT INTO runtime_channel_events(id,channel_name,from_bundle_id,to_bundle_id,idempotency_key,created_at) VALUES (?,?,?,?,?,?)",
            (f"channel_event_{uuid.uuid4().hex}", channel, current["bundle_id"] if current else None, bundle_id, idempotency_key, now),
        )

    @staticmethod
    def _event(connection, candidate_id: str | None, event_type: str, actor: str, data: dict[str, Any], key: str) -> None:
        connection.execute(
            "INSERT INTO evolution_events(event_id,candidate_id,type,actor,data_json,idempotency_key,occurred_at) VALUES (?,?,?,?,?,?,?)",
            (f"evolution_event_{uuid.uuid4().hex}", candidate_id, event_type, actor, _json(data), key, _now()),
        )

    def _candidate_row(self, connection, candidate_id: str, owner_id: str) -> dict[str, Any]:
        row = self._candidate_db(connection, candidate_id, owner_id)
        result = self._candidate(row)
        result["evidence_count"] = len(result["experience_ids"])
        evaluation = connection.execute("SELECT * FROM evolution_evaluations WHERE id=?", (row["current_evaluation_id"],)).fetchone() if row["current_evaluation_id"] else None
        result["evaluation"] = self._evaluation(evaluation) if evaluation else None
        deployment = connection.execute("SELECT * FROM canary_deployments WHERE id=?", (row["deployment_id"],)).fetchone() if row["deployment_id"] else None
        if deployment:
            counts = connection.execute("SELECT COUNT(*) total,SUM(cohort='challenger') challenger FROM canary_exposures WHERE deployment_id=?", (deployment["id"],)).fetchone()
            result["canary"] = {"id":deployment["id"],"allocation":deployment["allocation_percent"],"sample_size":int(counts["challenger"] or 0),"total_exposures":int(counts["total"] or 0),"status":deployment["status"]}
        else: result["canary"] = None
        return result

    @staticmethod
    def _candidate_db(connection, candidate_id: str, owner_id: str):
        row = connection.execute("SELECT * FROM evolution_candidates WHERE id=? AND owner_id=?", (candidate_id, owner_id)).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return row

    @staticmethod
    def _experience(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _candidate(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["experience_ids"] = json.loads(result.pop("experience_ids_json"))
        result["proposed_content"] = json.loads(result.pop("proposed_content_json"))
        result["permission_diff"] = json.loads(result.pop("permission_diff_json"))
        return result

    @staticmethod
    def _evaluation(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["deterministic_pass"] = bool(result["deterministic_pass"])
        result["checks"] = json.loads(result.pop("checks_json"))
        result["metrics"] = json.loads(result.pop("metrics_json"))
        return result

    @staticmethod
    def _decision(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _deployment(row: Any) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _exposure(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["success"] = bool(result["success"])
        result["safety_pass"] = bool(result["safety_pass"])
        return result
