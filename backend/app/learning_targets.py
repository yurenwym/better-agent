"""Target adapters: the only place that knows how to turn a draft into a real asset.

Each adapter owns one V3 target and exposes the same five operations, so the
evaluation and promotion stages never branch on the target:

    create_candidate()  draft      -> a stored, non-activated candidate
    validate_candidate() candidate -> rule-evaluation verdict (no model involved)
    evaluate()          candidate  -> the target-specific evaluation pipeline
    promote()           candidate  -> activation, gated by the harness
    rollback()          candidate  -> return to the previous known-good state

Adapters never decide *whether* to promote; they execute a decision the
Promotion Gate already made.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import zipfile
from typing import Any, ClassVar, Mapping

from .learning_agent import BehaviorDraft, CandidateDraft, MemoryDraft, SkillDraft
from .learning_contract import (
    BEHAVIOR_MODEL_POLICY,
    BEHAVIOR_PROMPT,
    TARGET_BEHAVIOR,
    TARGET_MEMORY,
    TARGET_SKILL,
)

# Behavior surfaces this build can actually release. `EvolutionService` only
# carries `prompt` and `policy` candidates, so the other subtypes are refused
# loudly instead of being silently dropped.
BEHAVIOR_SURFACE_PATHS: dict[str, tuple[str, ...]] = {
    BEHAVIOR_PROMPT: ("prompts",),
    BEHAVIOR_MODEL_POLICY: ("model_routing",),
}
BEHAVIOR_CANDIDATE_TYPES: dict[str, str] = {
    BEHAVIOR_PROMPT: "prompt",
    BEHAVIOR_MODEL_POLICY: "policy",
}

# Nothing a learning candidate proposes may touch these manifest surfaces.
FROZEN_MANIFEST_KEYS = (
    "core_policy", "gate_policy", "promotion_policy", "permissions", "security_policy",
    "tenant_isolation", "approval_rules", "secret_policy",
)

EXECUTABLE_MARKERS = ("```", "<script", "#!/", "subprocess.", "os.system(", "exec(", "eval(")
SKILL_TRIGGER_MARKERS = ("适用：", "适用:")


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _manifest_diff(base: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    """Mirror of `evolution._manifest_diff`: a shallow top-level key diff.

    This must stay byte-identical in behaviour to the EvolutionService version,
    because `propose_candidate` compares the proposed content against its own
    diff and rejects any mismatch.
    """
    return {key: target.get(key) for key in base.keys() | target.keys() if base.get(key) != target.get(key)}


def _deep_merge(manifest: dict[str, Any], path: tuple[str, ...], change: Mapping[str, Any]) -> dict[str, Any]:
    node = manifest
    for key in path[:-1]:
        node = node.setdefault(key, {})
    leaf = node.setdefault(path[-1], {})
    if not isinstance(leaf, Mapping):
        raise ValueError(f"manifest path {'.'.join(path)} is not an object")
    node[path[-1]] = _merge_into(dict(leaf), change)
    return manifest


def _merge_into(target: dict[str, Any], change: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in change.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), Mapping):
            target[key] = _merge_into(dict(target[key]), value)
        else:
            target[key] = value
    return target


class LearningTargetAdapter:
    """Uniform surface every target implements."""

    target: ClassVar[str] = ""

    def __init__(self, db: Any) -> None:
        self.db = db

    def create_candidate(self, draft: CandidateDraft, *, owner_id: str, job_id: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def validate_candidate(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def evaluate(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        from .learning_eval import evaluate_candidate

        return evaluate_candidate(self, candidate, owner_id=owner_id, **kwargs)

    def promote(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def rollback(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _verdict(checks: Mapping[str, Any], reason: str = "") -> dict[str, Any]:
        failed = [name for name, value in checks.items() if not value]
        return {"pass": not failed, "checks": {name: bool(value) for name, value in checks.items()},
                "reason": reason or (f"rule evaluation failed: {', '.join(failed)}" if failed else "")}


class MemoryTargetAdapter(LearningTargetAdapter):
    """Memory candidates become proposals; only the harness accepts them."""

    target = TARGET_MEMORY

    def __init__(self, db: Any, memory: Any) -> None:
        super().__init__(db)
        self.memory = memory

    def create_candidate(self, draft: CandidateDraft, *, owner_id: str, job_id: str,
                         target_entry_id: str | None = None, base_revision_id: str | None = None,
                         source_thread_id: str | None = None, source_run_id: str | None = None,
                         **kwargs: Any) -> dict[str, Any]:
        if not isinstance(draft, MemoryDraft):
            raise ValueError("MemoryTargetAdapter requires a MemoryDraft")
        proposal = self.memory.propose(
            owner_id=owner_id, operation=draft.operation, kind=draft.kind,
            scope_type=draft.proposed_change["scope_type"], scope_id=draft.proposed_change["scope_id"],
            content=draft.content, confidence=1.0 if draft.evidence_refs else 0.5,
            evidence_refs=list(draft.evidence_refs), idempotency_key=f"{job_id}:memory-proposal",
            target_entry_id=target_entry_id, base_revision_id=base_revision_id,
            reason=draft.generalizable_lesson[:400], source_thread_id=source_thread_id, source_run_id=source_run_id,
        )
        return {"target": self.target, "candidate_id": proposal.id, "proposal_id": proposal.id,
                "status": proposal.status, "operation": proposal.operation, "kind": proposal.kind,
                "scope_type": proposal.scope_type, "scope_id": proposal.scope_id, "content": proposal.content,
                "evidence_digest": draft.evidence_digest, "draft": draft.to_dict(),
                "target_entry_id": target_entry_id, "base_revision_id": base_revision_id}

    def validate_candidate(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        from .memory_v2 import SECRET_RE

        proposal = self.memory.get_proposal(candidate["proposal_id"], owner_id)
        content = proposal.content or ""
        with self.db.connection() as connection:
            entry = connection.execute(
                "SELECT id,owner_id,status,scope_type,scope_id FROM memory_entries WHERE owner_id=? AND current_revision_id=?",
                (owner_id, proposal.base_revision_id),
            ).fetchone() if proposal.base_revision_id else None
        checks = {
            "owner_matches": proposal.owner_id == owner_id,
            "scope_declared": bool(proposal.scope_type) and (proposal.scope_type != "project" or bool(proposal.scope_id)),
            "content_present": bool(content.strip()),
            "no_secret": not SECRET_RE.search(content),
            "evidence_verified": proposal.evidence_state == "VERIFIED",
            "evidence_present": bool(proposal.evidence),
            "target_active": entry is None or entry["status"] == "ACTIVE",
            "target_scope_matches": entry is None or (entry["scope_type"], entry["scope_id"]) == (proposal.scope_type, proposal.scope_id),
        }
        return self._verdict(checks)

    def promote(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        # Recheck the exact source text immediately before accepting a proposal.
        with self.db.connection() as connection:
            for source in candidate.get("user_sources", []):
                row = connection.execute("SELECT m.content FROM thread_messages m JOIN threads t ON t.id=m.thread_id "
                    "WHERE m.id=? AND m.role='user' AND t.owner_id=? AND t.deleted_at IS NULL", (source["id"], owner_id)).fetchone()
                if row is None or hashlib.sha256(row["content"].encode("utf-8")).hexdigest() != source["content_hash"]:
                    raise ValueError("Memory user evidence changed or was revoked")
        proposal = self.memory.get_proposal(candidate["proposal_id"], owner_id)
        accepted = self.memory.decide_proposal(
            proposal.id, owner_id, True, f"{candidate['candidate_id']}:promote",
            expected_version=proposal.version, authority=kwargs.get("authority", "learning_promotion"),
        )
        return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": accepted.status,
                "revision_id": accepted.accepted_revision_id, "entry_id": accepted.target_entry_id}

    def rollback(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        entry_id = candidate.get("entry_id") or kwargs.get("entry_id")
        if not entry_id:
            return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": "NOT_APPLIED"}
        entry = self.memory.get(entry_id, owner_id)
        if entry.revision_no <= 1:
            self.memory.set_status(entry_id, owner_id, "ARCHIVED", idempotency_key=f"{candidate['candidate_id']}:rollback")
            return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": "ARCHIVED", "entry_id": entry_id}
        restored = self.memory.rollback(entry_id, owner_id, entry.revision_no - 1, entry.revision_id)
        return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": "ROLLED_BACK",
                "entry_id": entry_id, "revision_id": restored.revision_id}


def skill_package(draft: SkillDraft, *, version: str) -> bytes:
    """Build the installable package for a skill draft."""
    manifest = {
        "schema_version": 1, "name": draft.name, "version": version,
        "title": draft.proposed_change.get("title") or draft.name,
        "description": draft.proposed_change.get("description") or "",
        "kind": draft.proposed_change.get("kind", "instruction_only"),
        "requested_tools": [], "connectors": [],
        "phases": list(draft.proposed_change.get("phases") or ["conversation"]),
        "entry_document": "SKILL.md",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in (("skill.json", json.dumps(manifest, ensure_ascii=False)),
                           ("SKILL.md", draft.proposed_change["content"])):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(info, body)
    return buffer.getvalue()


class SkillTargetAdapter(LearningTargetAdapter):
    """Skill candidates are stored INSTALLED with no grant and never auto-enabled."""

    target = TARGET_SKILL

    def __init__(self, db: Any, platform: Any) -> None:
        super().__init__(db)
        self.platform = platform

    def create_candidate(self, draft: CandidateDraft, *, owner_id: str, job_id: str, **kwargs: Any) -> dict[str, Any]:
        if not isinstance(draft, SkillDraft):
            raise ValueError("SkillTargetAdapter requires a SkillDraft")
        if owner_id != self.platform.owner_id:
            from .skill_platform import SkillCandidateRejected
            raise SkillCandidateRejected("skill platform owner mismatch")
        # Content-addressed prereleases avoid read-max/increment races and reuse
        # identical packages. Production selection never relies on version order.
        content_digest = hashlib.sha256(skill_package(draft, version="1.0.0")).hexdigest()
        version = str(kwargs.get("version") or f"1.0.0-learning.{content_digest}")
        stored = self.platform.store_candidate(skill_package(draft, version=version), job_id=job_id)
        return {"target": self.target, "candidate_id": stored["version_id"], "version_id": stored["version_id"],
                "base_version_id": stored.get("base_version_id"),
                "skill_id": stored["skill_id"], "name": stored["name"], "version": stored["version"],
                "status": stored["status"], "grant_status": stored["grant_status"],
                "requested_tools": stored["requested_tools"], "connectors": stored["connectors"],
                "package_digest": stored["package_digest"], "content": stored["content"],
                "evidence_digest": draft.evidence_digest, "draft": draft.to_dict()}

    def validate_candidate(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        version = self.platform.version(candidate["version_id"])
        content = version["content"] or ""
        markers = [marker for marker in EXECUTABLE_MARKERS if marker in content]
        checks = {
            "manifest_valid": version["status"] == "INSTALLED",
            "instruction_only": version["kind"] == "instruction_only",
            "no_requested_tools": not version["requested_tools"],
            "no_connectors": not version["connectors"],
            "no_grant": version["grant_status"] in (None, "NONE"),
            "not_enabled": version["status"] != "ENABLED",
            "exit_condition": "退出：" in content,
            "trigger_condition": any(marker in content for marker in SKILL_TRIGGER_MARKERS),
            "no_executable_code": not markers,
            "name_is_slug": bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", version["name"] or "")),
        }
        reason = f"executable markers present: {markers}" if markers else ""
        return self._verdict(checks, reason)

    def promote(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        """Enable an already-evaluated version. Grants stay empty: no permission growth."""
        from .learning import now as _now

        version_id = candidate["version_id"]
        if owner_id != self.platform.owner_id:
            raise ValueError("skill platform owner mismatch")
        with self.db.transaction() as connection:
            suffix = " FOR UPDATE" if self.db.backend == "postgresql" else ""
            skill = connection.execute("SELECT * FROM skills WHERE id=? AND owner_id=?" + suffix,
                                       (candidate["skill_id"], owner_id)).fetchone()
            event_key = f"{version_id}:learning-enable"
            prior = connection.execute("SELECT 1 FROM skill_events WHERE idempotency_key=?", (event_key,)).fetchone()
            if prior:
                return {"target": self.target, "candidate_id": version_id, "status": "ENABLED", "version_id": version_id, "granted_tools": []}
            version = self.platform.version(version_id, connection=connection)
            if (skill is None or skill["status"] == "UNINSTALLED" or version["skill_id"] != skill["id"]
                    or version["status"] != "INSTALLED" or version["requested_tools"] or version["connectors"]):
                raise ValueError("skill candidate is no longer eligible for promotion")
            previous = skill["default_version_id"]
            if previous != candidate.get("base_version_id"):
                raise ValueError("skill baseline changed after candidate creation")
            connection.execute("UPDATE skill_versions SET status='ENABLED' WHERE id=?", (version_id,))
            connection.execute(
                "UPDATE skills SET status='ENABLED',default_version_id=?,updated_at=? WHERE id=? AND status<>'UNINSTALLED'",
                (version_id, _now(), candidate["skill_id"]))
            self.platform._event(connection, skill["id"], version_id, "skill.learning_enabled",
                                 {"previous_version_id": previous}, event_key, actor="learning")
        return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": "ENABLED",
                "version_id": version_id, "granted_tools": []}

    def rollback(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        version_id = candidate.get("version_id")
        if not version_id:
            return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": "NOT_APPLIED"}
        if owner_id != self.platform.owner_id:
            raise ValueError("skill platform owner mismatch")
        with self.db.transaction() as connection:
            suffix = " FOR UPDATE" if self.db.backend == "postgresql" else ""
            version = self.platform.version(version_id, connection=connection)
            skill = connection.execute("SELECT * FROM skills WHERE id=? AND owner_id=?" + suffix,
                                       (version["skill_id"], owner_id)).fetchone()
            event_key = f"{version_id}:learning-rollback"
            if not connection.execute("SELECT 1 FROM skill_events WHERE idempotency_key=?", (event_key,)).fetchone():
                if skill is None or skill["default_version_id"] != version_id:
                    raise ValueError("skill default changed after promotion")
                event = connection.execute("SELECT data_json FROM skill_events WHERE idempotency_key=?",
                                           (f"{version_id}:learning-enable",)).fetchone()
                previous = json.loads(event["data_json"]).get("previous_version_id") if event else None
                if previous and self.platform.version(previous, connection=connection)["status"] != "ENABLED":
                    raise ValueError("previous skill version is no longer enabled")
                connection.execute("UPDATE skill_versions SET status='DISABLED' WHERE id=?", (version_id,))
                connection.execute("UPDATE skills SET default_version_id=?,status=? WHERE id=?",
                                   (previous, "ENABLED" if previous else "DISABLED", skill["id"]))
                self.platform._event(connection, skill["id"], version_id, "skill.learning_rolled_back",
                                     {"restored_version_id": previous}, event_key, actor="learning")
        return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": "DISABLED",
                "version_id": version_id}


class BehaviorTargetAdapter(LearningTargetAdapter):
    """Behavior candidates go through the existing Evolution release lifecycle."""

    target = TARGET_BEHAVIOR

    def __init__(self, db: Any, bundles: Any, evolution: Any) -> None:
        super().__init__(db)
        self.bundles, self.evolution = bundles, evolution

    def create_candidate(self, draft: CandidateDraft, *, owner_id: str, job_id: str, **kwargs: Any) -> dict[str, Any]:
        if not isinstance(draft, BehaviorDraft):
            raise ValueError("BehaviorTargetAdapter requires a BehaviorDraft")
        subtype = draft.subtype
        if subtype not in BEHAVIOR_SURFACE_PATHS:
            raise ValueError(
                f"behavior subtype {subtype!r} has no release adapter in this build; "
                f"supported: {sorted(BEHAVIOR_SURFACE_PATHS)}")
        change = draft.proposed_change["change"]
        forbidden = sorted(set(change) & set(FROZEN_MANIFEST_KEYS))
        if forbidden:
            raise ValueError(f"a behavior candidate may not modify {forbidden}")
        base = self.bundles.active(kwargs.get("channel", "stable"))
        manifest = _deep_merge(copy.deepcopy(base.manifest), BEHAVIOR_SURFACE_PATHS[subtype], change)
        target = self.bundles.ensure(manifest)
        proposed = _manifest_diff(base.manifest, target.manifest)
        if not proposed:
            raise ValueError("the proposed behavior change does not alter the runtime bundle")
        candidate = self.evolution.propose_candidate(
            candidate_type=BEHAVIOR_CANDIDATE_TYPES[subtype], experience_ids=list(draft.experience_ids),
            base_bundle_id=base.id, target_bundle_id=target.id, proposed_content=proposed,
            permission_diff={"added": [], "removed": []}, reason=draft.generalizable_lesson[:400],
            idempotency_key=f"{job_id}:behavior-candidate", owner_id=owner_id,
            problem_fingerprint=draft.problem[:200], root_cause_hypothesis=draft.root_cause[:400],
            confidence_limitations="; ".join(draft.risks)[:400], expected_metrics=dict(draft.expected_effect),
            risks=list(draft.risks),
        )
        return {"target": self.target, "candidate_id": candidate["id"], "subtype": subtype,
                "base_bundle_id": base.id, "target_bundle_id": target.id, "diff": proposed,
                "permission_diff": {"added": [], "removed": []}, "evidence_digest": draft.evidence_digest,
                "draft": draft.to_dict(), "status": candidate["status"]}

    def validate_candidate(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        stored = self.evolution.get_candidate(candidate["candidate_id"], owner_id)
        base = json.loads(self._bundle_manifest(stored["base_bundle_id"]))
        target = json.loads(self._bundle_manifest(stored["target_bundle_id"]))
        diff = stored["proposed_content"]
        permission_diff = stored["permission_diff"]
        touched = {path.split(".")[0] for path in diff}
        expected_root = BEHAVIOR_SURFACE_PATHS.get(candidate.get("subtype", ""), ())
        checks = {
            "candidate_ready": stored["status"] in {"READY_FOR_EVAL", "EVALUATED"},
            "no_permission_expansion": not permission_diff.get("added"),
            "permissions_subset": set(target.get("permissions", [])).issubset(set(base.get("permissions", []))),
            "core_policy_unchanged": base.get("core_policy") == target.get("core_policy"),
            "frozen_surfaces_untouched": not (touched & set(FROZEN_MANIFEST_KEYS)),
            "single_surface": bool(touched) and touched.issubset({expected_root[0]} if expected_root else touched),
            "diff_matches_bundle": diff == _manifest_diff(base, target),
            "subtype_supported": candidate.get("subtype") in BEHAVIOR_SURFACE_PATHS,
        }
        return self._verdict(checks)

    def _bundle_manifest(self, bundle_id: str) -> str:
        with self.db.connection() as connection:
            row = connection.execute("SELECT manifest_json FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone()
        if row is None:
            raise KeyError(bundle_id)
        return row["manifest_json"]

    def promote(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        """Hand the promotion to EvolutionService; this adapter only routes."""
        stored = self.evolution.get_candidate(candidate["candidate_id"], owner_id)
        decision = self.evolution.promote(
            stored["id"], expected_version=stored["version"],
            idempotency_key=f"{candidate['candidate_id']}:promote", owner_id=owner_id)
        return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": decision.get("status"),
                "bundle_id": decision.get("target_bundle_id") or stored["target_bundle_id"]}

    def rollback(self, candidate: Mapping[str, Any], *, owner_id: str, **kwargs: Any) -> dict[str, Any]:
        stored = self.evolution.get_candidate(candidate["candidate_id"], owner_id)
        result = self.evolution.rollback(
            stored["id"], expected_version=stored["version"],
            reason=kwargs.get("reason", "learning canary rollback"),
            idempotency_key=f"{candidate['candidate_id']}:rollback", owner_id=owner_id)
        return {"target": self.target, "candidate_id": candidate["candidate_id"], "status": result.get("status"),
                "bundle_id": result.get("bundle_id")}


def build_adapters(*, db: Any, memory: Any, platform: Any, bundles: Any, evolution: Any) -> dict[str, LearningTargetAdapter]:
    return {
        TARGET_MEMORY: MemoryTargetAdapter(db, memory),
        TARGET_SKILL: SkillTargetAdapter(db, platform),
        TARGET_BEHAVIOR: BehaviorTargetAdapter(db, bundles, evolution),
    }


__all__ = [
    "BEHAVIOR_CANDIDATE_TYPES", "BEHAVIOR_SURFACE_PATHS", "BehaviorTargetAdapter", "FROZEN_MANIFEST_KEYS",
    "LearningTargetAdapter", "MemoryTargetAdapter", "SkillTargetAdapter", "build_adapters", "skill_package",
]
