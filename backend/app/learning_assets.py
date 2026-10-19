"""Small dependency graph and frozen request references; asset bodies stay in their stores."""
from __future__ import annotations

import json
import re
from .learning import LearningConflict, digest, encode, now

STATEMENTS = (
    "ALTER TABLE memory_entries ADD COLUMN applicability_json TEXT NOT NULL DEFAULT '{}'",
    "CREATE TABLE learning_dependencies (owner_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES learning_jobs(id), "
    "PRIMARY KEY(owner_id,source_kind,source_id,job_id))",
    "CREATE TABLE learning_snapshots (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_kind TEXT NOT NULL, task_id TEXT NOT NULL, "
    "assets_json TEXT NOT NULL, digest TEXT NOT NULL, invalidated_at TEXT, reason TEXT, created_at TEXT NOT NULL, UNIQUE(owner_id,task_kind,task_id))",
)


def scope_matches(applicability, *, program_id=None, run_id=None, turn_id=None):
    return all(value == {"program_id": program_id, "run_id": run_id, "turn_id": turn_id}.get(key) for key, value in applicability.items())


def _asset_readable(*, owner_id: str, resource_owner_id: str, asset_id: str, source_valid: bool) -> bool:
    """Read decision for one durable asset row. Denial performs no write."""
    from .policy_adapters import decide_asset
    from .policy_engine import PolicyAction

    decision = decide_asset(
        "read", owner_id=owner_id, resource_owner_id=resource_owner_id, asset_id=asset_id,
        source_valid=source_valid, approval_required=False, approval_granted=False,
    )
    return decision is not None and decision.action is PolicyAction.ALLOW


class LearningAssets:
    def __init__(self, learning):
        self.learning, self.db = learning, learning.db

    def depend(self, job_id, owner_id, sources, *, connection):
        if connection.execute("SELECT 1 FROM learning_jobs WHERE id=? AND owner_id=?", (job_id, owner_id)).fetchone() is None:
            raise LearningConflict("dependency belongs to another owner")
        for kind, source_id in sources:
            if kind not in {"thread_message", "experience", "goal_review", "feedback", "job", "revision", "skill", "bundle"} or not source_id:
                raise ValueError("invalid learning dependency")
            if kind == "job":
                if source_id == job_id or connection.execute("SELECT 1 FROM learning_jobs WHERE id=? AND owner_id=? AND status='APPLIED'", (source_id, owner_id)).fetchone() is None:
                    raise LearningConflict("invalid parent learning job")
            connection.execute("INSERT INTO learning_dependencies(owner_id,source_kind,source_id,job_id) VALUES (?,?,?,?) ON CONFLICT DO NOTHING", (owner_id, kind, source_id, job_id))

    def freeze(self, owner_id, task_kind, task_id, assets):
        identity = digest([owner_id, task_kind, task_id])
        with self.db.transaction() as connection:
            connection.execute("INSERT INTO learning_snapshots(id,owner_id,task_kind,task_id,assets_json,digest,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(owner_id,task_kind,task_id) DO NOTHING",
                               (identity, owner_id, task_kind, task_id, encode(assets), digest(assets), now()))
            row = connection.execute("SELECT * FROM learning_snapshots WHERE id=?", (identity,)).fetchone()
            if row["invalidated_at"]:
                raise LearningConflict("asset snapshot revoked")
            return {**dict(row), "assets": json.loads(row["assets_json"])}

    def freeze_request(self, connection, context, request, invocation_id):
        rendered = "\n".join(str(message.get("content", "")) for message in request.messages)
        assets = []
        if context.runtime_bundle_id:
            assets.append({"kind": "bundle", "id": context.runtime_bundle_id, "selected": True, "included": True})
        # Compiler carries typed revision references in its structured request.
        if context.purpose == "compile_goal_program":
            for revision_id in set(re.findall(r"memory_revision_[a-f0-9]+", rendered)):
                revision = connection.execute(
                    "SELECT r.content_hash, e.owner_id, e.status, e.valid_until FROM memory_revisions r "
                    "JOIN memory_entries e ON e.id=r.entry_id WHERE r.id=? AND e.owner_id=?",
                    (revision_id, context.owner_id),
                ).fetchone()
                if revision is None:
                    raise LearningConflict("request memory revoked")
                active = revision["status"] == "ACTIVE" and (revision["valid_until"] is None or revision["valid_until"] > now())
                if not _asset_readable(owner_id=context.owner_id, resource_owner_id=revision["owner_id"], asset_id=revision_id, source_valid=active):
                    raise LearningConflict("request memory revoked")
                assets.append({"kind": "revision", "id": revision_id, "selected": True, "included": True, "content_hash": revision["content_hash"], "consumer": "typed_compiler_constraint"})
        for task_id in filter(None, (context.turn_id, context.run_id)):
            binding = connection.execute("SELECT version_ids_json,grant_snapshots_json,snapshot_digest FROM skill_bindings WHERE owner_id=? AND binding_type='RUN' AND binding_id=?",
                                         (context.owner_id, task_id)).fetchone()
            if binding:
                for version_id in json.loads(binding["version_ids_json"]):
                    skill = connection.execute(
                        "SELECT v.content, v.package_digest, s.owner_id FROM skill_versions v "
                        "JOIN skills s ON s.id=v.skill_id WHERE v.id=?",
                        (version_id,),
                    ).fetchone()
                    if skill is None:
                        raise LearningConflict("request skill revoked")
                    if not _asset_readable(owner_id=context.owner_id, resource_owner_id=skill["owner_id"], asset_id=version_id, source_valid=True):
                        raise LearningConflict("request skill revoked")
                    assets.append({"kind": "skill", "id": version_id, "selected": True, "included": skill["content"] in rendered,
                                   "package_digest": skill["package_digest"], "grant_snapshot": json.loads(binding["grant_snapshots_json"]).get(version_id),
                                   "binding_digest": binding["snapshot_digest"]})
            pins = connection.execute("SELECT revision_ids_json,rendered_hash FROM memory_context_pins WHERE owner_id=? AND parent_id=? AND invalidated_at IS NULL",
                                      (context.owner_id, task_id)).fetchall()
            for pin in pins:
                for revision_id in json.loads(pin["revision_ids_json"]):
                    revision = connection.execute(
                        "SELECT r.content, r.content_hash, e.owner_id FROM memory_revisions r "
                        "JOIN memory_entries e ON e.id=r.entry_id WHERE r.id=?",
                        (revision_id,),
                    ).fetchone()
                    if revision is None or not _asset_readable(owner_id=context.owner_id, resource_owner_id=revision["owner_id"], asset_id=revision_id, source_valid=True):
                        raise LearningConflict("request memory revoked")
                    assets.append({"kind": "revision", "id": revision_id, "selected": True, "included": revision["content"] in rendered,
                                   "content_hash": revision["content_hash"], "pin_digest": pin["rendered_hash"]})
        identity = digest([context.owner_id, "invocation", invocation_id])
        connection.execute("INSERT INTO learning_snapshots(id,owner_id,task_kind,task_id,assets_json,digest,created_at) VALUES (?,?,'invocation',?,?,?,?)",
                           (identity, context.owner_id, invocation_id, encode(assets), digest({"assets": assets, "messages": request.messages}), now()))

    def assert_request_active(self, invocation_id, owner_id):
        with self.db.connection() as connection:
            row = connection.execute("SELECT invalidated_at,assets_json FROM learning_snapshots WHERE owner_id=? AND task_kind='invocation' AND task_id=?", (owner_id, invocation_id)).fetchone()
            if row is None:
                return
            if row["invalidated_at"]:
                raise LearningConflict("request asset snapshot revoked")
            for asset in json.loads(row["assets_json"]):
                if not asset.get("included"):
                    continue
                if asset["kind"] == "revision":
                    loaded = connection.execute(
                        "SELECT e.owner_id, e.status, e.valid_until FROM memory_revisions r "
                        "JOIN memory_entries e ON e.id=r.entry_id WHERE r.id=?",
                        (asset["id"],),
                    ).fetchone()
                    if loaded is None:
                        raise LearningConflict("request memory revoked")
                    active = loaded["status"] == "ACTIVE" and (loaded["valid_until"] is None or loaded["valid_until"] > now())
                    if not _asset_readable(owner_id=owner_id, resource_owner_id=loaded["owner_id"], asset_id=asset["id"], source_valid=active):
                        raise LearningConflict("request memory revoked")
                elif asset["kind"] == "skill":
                    loaded = connection.execute(
                        "SELECT s.owner_id FROM skill_versions v JOIN skills s ON s.id=v.skill_id WHERE v.id=?",
                        (asset["id"],),
                    ).fetchone()
                    if loaded is None or not _asset_readable(
                        owner_id=owner_id, resource_owner_id=loaded["owner_id"], asset_id=asset["id"], source_valid=True,
                    ):
                        raise LearningConflict("request skill revoked")
                    from .skill_platform import SkillPlatform
                    item = SkillPlatform(self.db, self.learning.skill_root, owner_id).version(asset["id"], connection=connection)
                    source_valid = item["status"] == "ENABLED" and item["grant_status"] == "ACTIVE"
                    if not _asset_readable(
                        owner_id=owner_id, resource_owner_id=loaded["owner_id"], asset_id=asset["id"], source_valid=source_valid,
                    ):
                        raise LearningConflict("request skill revoked")

    def revoke(self, owner_id, source_kind, source_id, reason):
        pending, visited = [(source_kind, source_id)], set()
        while pending:
            kind, identity = pending.pop()
            if (kind, identity) in visited:
                continue
            visited.add((kind, identity))
            with self.db.transaction() as connection:
                rows = connection.execute("SELECT j.* FROM learning_dependencies d JOIN learning_jobs j ON j.id=d.job_id WHERE d.owner_id=? AND d.source_kind=? AND d.source_id=?",
                                          (owner_id, kind, identity)).fetchall()
                pins = connection.execute("SELECT id,assets_json FROM learning_snapshots WHERE owner_id=? AND invalidated_at IS NULL", (owner_id,)).fetchall()
                for pin in pins:
                    if any(item.get("kind") == kind and item.get("id") == identity for item in json.loads(pin["assets_json"])):
                        connection.execute("UPDATE learning_snapshots SET invalidated_at=?,reason=? WHERE id=?", (now(), reason, pin["id"]))
            for row in rows:
                if row["status"] == "APPLIED":
                    self.learning.suspend(row["id"], owner_id, expected_version=row["version"], reason=reason)
                elif row["status"] in {"QUEUED", "RUNNING"}:
                    with self.db.transaction() as connection:
                        connection.execute("UPDATE learning_jobs SET status='SUSPENDED',reason=?,version=version+1,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=? AND version=?",
                                           (reason, now(), row["id"], row["version"]))
                pending.append(("job", row["id"]))
                if row["source_kind"] == "thread_message" and reason in {"source_thread_deleted", "memory_forgotten"}:
                    with self.db.transaction() as connection:
                        connection.execute("UPDATE memory_proposals SET content='',accepted_content=NULL WHERE request_idempotency_key=? AND owner_id=?",
                                           (row["id"] + ":proposal", owner_id))
                for change in json.loads(row["change_set_json"]):
                    asset_kind = {"memory": "revision", "skill": "skill", "prompt": "bundle", "task_policy": "bundle"}.get(change.get("asset_type"))
                    if asset_kind == "revision" and reason in {"source_thread_deleted", "memory_forgotten"}:
                        with self.db.connection() as connection:
                            entry = connection.execute("SELECT id FROM memory_entries WHERE owner_id=? AND current_revision_id=? AND status<>'PURGED'", (owner_id, change["after"])).fetchone()
                        if entry:
                            self.learning.memory.purge(entry[0], owner_id, idempotency_key="cascade-purge:" + row["id"])
                    if asset_kind and change.get("after"):
                        pending.append((asset_kind, change["after"]))
        return len(visited)
