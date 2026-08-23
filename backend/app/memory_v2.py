from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database


KINDS = {"preference", "constraint", "fact", "decision", "lesson"}
SCOPES = {"user", "project"}
SECRET_RE = re.compile(
    r"(?i)(api[_ -]?key|password|passwd|token|secret|private[_ -]?key)\s*[:=]\s*([^\s,;]+)"
)


@dataclass(frozen=True)
class MemoryEntry:
    id: str
    owner_id: str
    kind: str
    scope_type: str
    scope_id: str
    status: str
    content: str
    revision_id: str
    revision_no: int
    pinned: bool
    importance: float
    sensitivity: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryRevision:
    id: str
    entry_id: str
    revision_no: int
    operation: str
    content: str
    base_revision_id: str | None
    actor: str
    source_refs: tuple[str, ...]
    reason: str
    created_at: str


@dataclass(frozen=True)
class MemoryProposal:
    id: str
    owner_id: str
    operation: str
    target_entry_id: str | None
    base_revision_id: str | None
    kind: str
    scope_type: str
    scope_id: str
    content: str
    confidence: float
    status: str
    accepted_revision_id: str | None
    reason: str
    created_at: str


@dataclass(frozen=True)
class MemoryEpisode:
    id: str
    owner_id: str
    thread_id: str
    project_id: str | None
    start_message_seq: int
    end_message_seq: int
    summary: str
    sensitivity: str
    retrieval_policy: str
    status: str
    created_at: str


@dataclass(frozen=True)
class MemoryContextRequest:
    owner_id: str
    thread_id: str
    project_id: str | None
    query: str
    purpose: str = "conversation"
    semantic_token_budget: int = 1500
    episode_token_budget: int = 1000
    model_invocation_id: str | None = None
    parent_type: str = "thread"
    parent_id: str | None = None


@dataclass(frozen=True)
class MemoryContextBundle:
    rendered: str
    revision_ids: tuple[str, ...]
    episode_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    dropped: int
    token_count: int
    tokenizer_version: str
    renderer_version: str
    bundle_hash: str


class MemoryConflict(ValueError):
    pass


class MemoryStore:
    def __init__(self, db: Database, root: str | Path) -> None:
        self.db = db
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "projects").mkdir(exist_ok=True)

    def remember(
        self, owner_id: str, kind: str, scope_type: str, scope_id: str, content: str,
        idempotency_key: str, source_refs: list[str] | None = None, *, pinned: bool = False,
        importance: float = .5, sensitivity: str = "normal",
    ) -> MemoryEntry:
        content = _validate_content(content)
        _validate_kind_scope(kind, scope_type, scope_id)
        fingerprint = _fingerprint(content)
        now = _now()
        with self.db.transaction() as connection:
            audit = connection.execute(
                "SELECT aggregate_id FROM memory_audit_events WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if audit:
                return self._entry(audit["aggregate_id"], owner_id, connection)
            duplicate = connection.execute(
                "SELECT id FROM memory_entries WHERE owner_id=? AND scope_type=? AND scope_id=? "
                "AND canonical_fingerprint=? AND status='ACTIVE'",
                (owner_id, scope_type, scope_id, fingerprint),
            ).fetchone()
            if duplicate:
                entry = self._entry(duplicate["id"], owner_id, connection)
                self._audit(connection, owner_id, "entry", entry.id, idempotency_key, "remember_duplicate", "user")
                return entry
            entry_id = f"memory_{uuid.uuid4().hex}"
            revision_id = f"memory_revision_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO memory_entries(id,owner_id,kind,scope_type,scope_id,status,current_revision_id,canonical_fingerprint,pinned,importance,sensitivity,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'ACTIVE',?,?,?,?,?,?,?)",
                (entry_id, owner_id, kind, scope_type, scope_id, revision_id, fingerprint, int(pinned), importance, sensitivity, now, now),
            )
            connection.execute(
                "INSERT INTO memory_revisions(id,entry_id,revision_no,operation,content,content_hash,actor,source_refs_json,created_at) "
                "VALUES (?,?,1,'CREATE',?,?,?,?,?)",
                (revision_id, entry_id, content, _hash(content), "user", json.dumps(source_refs or []), now),
            )
            connection.execute("INSERT INTO memory_fts(entry_id,owner_id,content) VALUES (?,?,?)", (entry_id, owner_id, content))
            self._audit(connection, owner_id, "entry", entry_id, idempotency_key, "remember", "user")
            self._projection_intent(connection, owner_id, scope_type, scope_id, now)
        self.project(owner_id)
        return self.get(entry_id, owner_id)

    def edit(self, entry_id: str, owner_id: str, content: str, base_revision_id: str, *, actor: str = "user") -> MemoryEntry:
        content = _validate_content(content)
        now = _now()
        with self.db.transaction() as connection:
            current = self._entry(entry_id, owner_id, connection)
            if current.status != "ACTIVE" or current.revision_id != base_revision_id:
                raise MemoryConflict("memory head conflict")
            revision_id = f"memory_revision_{uuid.uuid4().hex}"
            revision_no = current.revision_no + 1
            connection.execute(
                "INSERT INTO memory_revisions(id,entry_id,revision_no,operation,content,content_hash,base_revision_id,actor,created_at) "
                "VALUES (?,?,?,'UPDATE',?,?,?,?,?)",
                (revision_id, entry_id, revision_no, content, _hash(content), base_revision_id, actor, now),
            )
            connection.execute(
                "UPDATE memory_entries SET current_revision_id=?,canonical_fingerprint=?,updated_at=? WHERE id=? AND owner_id=?",
                (revision_id, _fingerprint(content), now, entry_id, owner_id),
            )
            connection.execute("DELETE FROM memory_fts WHERE entry_id=?", (entry_id,))
            connection.execute("INSERT INTO memory_fts(entry_id,owner_id,content) VALUES (?,?,?)", (entry_id, owner_id, content))
            self._audit(connection, owner_id, "entry", entry_id, f"edit:{revision_id}", "edit", actor)
            self._projection_intent(connection, owner_id, current.scope_type, current.scope_id, now)
        self.project(owner_id)
        return self.get(entry_id, owner_id)

    def rollback(self, entry_id: str, owner_id: str, revision_no: int, base_revision_id: str) -> MemoryEntry:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT content FROM memory_revisions WHERE entry_id=? AND revision_no=?", (entry_id, revision_no)
            ).fetchone()
        if not row:
            raise KeyError(revision_no)
        result = self.edit(entry_id, owner_id, row["content"], base_revision_id, actor="user")
        with self.db.transaction() as connection:
            connection.execute("UPDATE memory_revisions SET operation='ROLLBACK' WHERE id=?", (result.revision_id,))
        return result

    def set_status(self, entry_id: str, owner_id: str, status: str) -> MemoryEntry:
        if status not in {"ACTIVE", "ARCHIVED"}:
            raise ValueError("invalid memory status")
        now = _now()
        with self.db.transaction() as connection:
            entry = self._entry(entry_id, owner_id, connection)
            connection.execute("UPDATE memory_entries SET status=?,updated_at=? WHERE id=? AND owner_id=?", (status, now, entry_id, owner_id))
            if status == "ARCHIVED": connection.execute("DELETE FROM memory_fts WHERE entry_id=?", (entry_id,))
            self._audit(connection, owner_id, "entry", entry_id, f"status:{entry_id}:{status}:{now}", status.lower(), "user")
            self._projection_intent(connection, owner_id, entry.scope_type, entry.scope_id, now)
        self.project(owner_id)
        return self.get(entry_id, owner_id)

    def purge(self, entry_id: str, owner_id: str) -> None:
        now = _now()
        with self.db.transaction() as connection:
            entry = self._entry(entry_id, owner_id, connection)
            connection.execute("DELETE FROM memory_revisions WHERE entry_id=?", (entry_id,))
            connection.execute("DELETE FROM memory_fts WHERE entry_id=?", (entry_id,))
            connection.execute(
                "UPDATE memory_entries SET status='PURGED',current_revision_id=NULL,canonical_fingerprint=?,updated_at=? WHERE id=? AND owner_id=?",
                (f"purged:{uuid.uuid4().hex}", now, entry_id, owner_id),
            )
            connection.execute(
                "UPDATE memory_context_pins SET invalidated_at=? WHERE owner_id=? AND revision_ids_json LIKE ? AND invalidated_at IS NULL",
                (now, owner_id, f'%{entry.revision_id}%'),
            )
            self._audit(connection, owner_id, "entry", entry_id, f"purge:{entry_id}:{now}", "purge", "user")
            self._projection_intent(connection, owner_id, entry.scope_type, entry.scope_id, now)
        self.project(owner_id)

    def propose(
        self, *, owner_id: str, operation: str, kind: str, scope_type: str, scope_id: str,
        content: str, confidence: float, evidence_refs: list[str], idempotency_key: str,
        target_entry_id: str | None = None, base_revision_id: str | None = None, reason: str = "",
    ) -> MemoryProposal:
        _validate_kind_scope(kind, scope_type, scope_id)
        content = _validate_content(content)
        if operation not in {"ADD", "UPDATE", "ARCHIVE"} or not 0 <= confidence <= 1:
            raise ValueError("invalid proposal")
        now = _now()
        with self.db.transaction() as connection:
            existing = connection.execute("SELECT id FROM memory_proposals WHERE request_idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                return self._proposal(existing["id"], owner_id, connection)
            proposal_id = f"memory_proposal_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO memory_proposals(id,owner_id,operation,target_entry_id,base_revision_id,kind,scope_type,scope_id,content,fingerprint,evidence_refs_json,evidence_hash,origin,confidence,reason,request_idempotency_key,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, owner_id, operation, target_entry_id, base_revision_id, kind, scope_type, scope_id, content,
                 _fingerprint(content), json.dumps(evidence_refs), _hash(json.dumps(evidence_refs)), "model", confidence, reason, idempotency_key, now),
            )
        return self.get_proposal(proposal_id, owner_id)

    def decide_proposal(self, proposal_id: str, owner_id: str, accept: bool, idempotency_key: str) -> MemoryProposal:
        with self.db.transaction() as connection:
            proposal = self._proposal(proposal_id, owner_id, connection)
            if proposal.status != "PENDING":
                if proposal.status == ("ACCEPTED" if accept else "REJECTED") and connection.execute(
                    "SELECT decision_idempotency_key FROM memory_proposals WHERE id=?", (proposal_id,)
                ).fetchone()[0] == idempotency_key:
                    return proposal
                raise ValueError("proposal already decided")
            now = _now()
            accepted_revision_id = None
            if accept:
                if proposal.operation == "ADD":
                    entry_id = f"memory_{uuid.uuid4().hex}"
                    accepted_revision_id = f"memory_revision_{uuid.uuid4().hex}"
                    connection.execute(
                        "INSERT INTO memory_entries(id,owner_id,kind,scope_type,scope_id,status,current_revision_id,canonical_fingerprint,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,'ACTIVE',?,?,?,?)",
                        (entry_id, owner_id, proposal.kind, proposal.scope_type, proposal.scope_id, accepted_revision_id, _fingerprint(proposal.content), now, now),
                    )
                    connection.execute(
                        "INSERT INTO memory_revisions(id,entry_id,revision_no,operation,content,content_hash,actor,created_at) VALUES (?,?,1,'CREATE',?,?,?,?)",
                        (accepted_revision_id, entry_id, proposal.content, _hash(proposal.content), "user-confirmed-model", now),
                    )
                    connection.execute("INSERT INTO memory_fts(entry_id,owner_id,content) VALUES (?,?,?)", (entry_id, owner_id, proposal.content))
                elif proposal.operation == "UPDATE":
                    current = self._entry(proposal.target_entry_id or "", owner_id, connection)
                    if current.revision_id != proposal.base_revision_id:
                        raise MemoryConflict("proposal base revision conflict")
                    accepted_revision_id = f"memory_revision_{uuid.uuid4().hex}"
                    connection.execute(
                        "INSERT INTO memory_revisions(id,entry_id,revision_no,operation,content,content_hash,base_revision_id,actor,created_at) VALUES (?,?,?,'UPDATE',?,?,?,?,?)",
                        (accepted_revision_id, current.id, current.revision_no + 1, proposal.content, _hash(proposal.content), current.revision_id, "user-confirmed-model", now),
                    )
                    connection.execute("UPDATE memory_entries SET current_revision_id=?,canonical_fingerprint=?,updated_at=? WHERE id=?", (accepted_revision_id, _fingerprint(proposal.content), now, current.id))
                    connection.execute("DELETE FROM memory_fts WHERE entry_id=?", (current.id,)); connection.execute("INSERT INTO memory_fts(entry_id,owner_id,content) VALUES (?,?,?)", (current.id, owner_id, proposal.content))
                else:
                    current = self._entry(proposal.target_entry_id or "", owner_id, connection)
                    if current.revision_id != proposal.base_revision_id:
                        raise MemoryConflict("proposal base revision conflict")
                    accepted_revision_id = current.revision_id
                    connection.execute("UPDATE memory_entries SET status='ARCHIVED',updated_at=? WHERE id=?", (now, current.id))
            connection.execute(
                "UPDATE memory_proposals SET status=?,decision_idempotency_key=?,accepted_revision_id=?,decided_at=? WHERE id=? AND status='PENDING'",
                ("ACCEPTED" if accept else "REJECTED", idempotency_key, accepted_revision_id, now, proposal_id),
            )
        self.project(owner_id)
        return self.get_proposal(proposal_id, owner_id)

    def save_episode(
        self, *, owner_id: str, thread_id: str, project_id: str | None, start_message_seq: int,
        end_message_seq: int, source_hash: str, summary: str, sensitivity: str = "normal",
        retrieval_policy: str | None = None, status: str = "ACTIVE",
    ) -> MemoryEpisode:
        summary = _redact(summary.strip())
        policy = retrieval_policy or ("thread" if sensitivity != "normal" or project_id is None else "project")
        now = _now()
        episode_id = f"episode_{uuid.uuid4().hex}"
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO memory_episodes(id,owner_id,thread_id,project_id,start_message_seq,end_message_seq,source_hash,summary,sensitivity,retrieval_policy,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (episode_id, owner_id, thread_id, project_id, start_message_seq, end_message_seq, source_hash, summary, sensitivity, policy, status, now),
            )
            row = connection.execute(
                "SELECT id FROM memory_episodes WHERE owner_id=? AND thread_id=? AND start_message_seq=? AND end_message_seq=? AND source_hash=?",
                (owner_id, thread_id, start_message_seq, end_message_seq, source_hash),
            ).fetchone()
        return self.get_episode(row["id"], owner_id)

    def list_entries(self, owner_id: str = "local-user", status: str | None = None) -> list[MemoryEntry]:
        query = "SELECT id FROM memory_entries WHERE owner_id=?"
        args: list[Any] = [owner_id]
        if status:
            query += " AND status=?"; args.append(status)
        query += " ORDER BY pinned DESC,importance DESC,updated_at DESC,id"
        with self.db.connection() as connection:
            rows = connection.execute(query, args).fetchall()
            return [self._entry(row["id"], owner_id, connection) for row in rows]

    def list_proposals(self, owner_id: str = "local-user", status: str | None = None) -> list[MemoryProposal]:
        query = "SELECT id FROM memory_proposals WHERE owner_id=?"
        args: list[Any] = [owner_id]
        if status: query += " AND status=?"; args.append(status)
        query += " ORDER BY created_at DESC,id"
        with self.db.connection() as connection:
            return [self._proposal(row["id"], owner_id, connection) for row in connection.execute(query, args)]

    def list_episodes(self, owner_id: str = "local-user", thread_id: str | None = None) -> list[MemoryEpisode]:
        query = "SELECT id FROM memory_episodes WHERE owner_id=? AND status IN ('ACTIVE','RAW_REFERENCE')"
        args: list[Any] = [owner_id]
        if thread_id: query += " AND thread_id=?"; args.append(thread_id)
        query += " ORDER BY created_at DESC,id"
        with self.db.connection() as connection:
            return [self._episode(row["id"], owner_id, connection) for row in connection.execute(query, args)]

    def get(self, entry_id: str, owner_id: str = "local-user") -> MemoryEntry:
        with self.db.connection() as connection: return self._entry(entry_id, owner_id, connection)

    def get_proposal(self, proposal_id: str, owner_id: str = "local-user") -> MemoryProposal:
        with self.db.connection() as connection: return self._proposal(proposal_id, owner_id, connection)

    def get_episode(self, episode_id: str, owner_id: str = "local-user") -> MemoryEpisode:
        with self.db.connection() as connection: return self._episode(episode_id, owner_id, connection)

    def revisions(self, entry_id: str, owner_id: str = "local-user") -> list[MemoryRevision]:
        self.get(entry_id, owner_id)
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM memory_revisions WHERE entry_id=? ORDER BY revision_no", (entry_id,)).fetchall()
        return [_revision(row) for row in rows]

    def project(self, owner_id: str) -> None:
        entries = self.list_entries(owner_id, "ACTIVE")
        grouped: dict[str, list[MemoryEntry]] = {}
        for entry in entries:
            if entry.scope_type == "project":
                path = f"projects/{_safe_id(entry.scope_id)}.md"
            elif entry.kind in {"preference", "constraint"}:
                path = "USER.md"
            else:
                path = "MEMORY.md"
            grouped.setdefault(path, []).append(entry)
        for relative in ["USER.md", "MEMORY.md", *sorted(grouped)]:
            selected = grouped.get(relative, [])
            title = "User profile" if relative == "USER.md" else "Long-term memory"
            if relative.startswith("projects/"): title = f"Project memory: {selected[0].scope_id}" if selected else "Project memory"
            content = f"# {title}\n\n" + "".join(f"- [{item.kind}] {item.content}\n" for item in selected)
            self._atomic_write(relative, content)
        with self.db.transaction() as connection:
            connection.execute("UPDATE memory_projection_intents SET state='COMMITTED',finished_at=? WHERE owner_id=? AND state='PENDING'", (_now(), owner_id))

    def recover_projections(self) -> None:
        with self.db.connection() as connection:
            owners = [row[0] for row in connection.execute("SELECT DISTINCT owner_id FROM memory_projection_intents WHERE state IN ('PENDING','FAILED')")]
        for owner in owners: self.project(owner)

    def _atomic_write(self, relative: str, content: str) -> None:
        path = (self.root / relative).resolve()
        if self.root not in path.parents: raise ValueError("unsafe memory projection path")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".memory-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content.rstrip("\n") + "\n"); handle.flush(); os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name): os.unlink(name)

    def _projection_intent(self, connection, owner_id: str, scope_type: str, scope_id: str, now: str) -> None:
        relative = f"projects/{_safe_id(scope_id)}.md" if scope_type == "project" else "USER.md"
        connection.execute(
            "INSERT INTO memory_projection_intents(id,owner_id,path,target_hash,state,created_at) VALUES (?,?,?,?, 'PENDING',?)",
            (f"projection_{uuid.uuid4().hex}", owner_id, relative, "pending", now),
        )

    @staticmethod
    def _audit(connection, owner_id: str, aggregate_type: str, aggregate_id: str, key: str, operation: str, actor: str) -> None:
        seq = int(connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM memory_audit_events WHERE owner_id=?", (owner_id,)).fetchone()[0])
        connection.execute(
            "INSERT INTO memory_audit_events(owner_id,seq,event_id,aggregate_type,aggregate_id,idempotency_key,operation,actor,occurred_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (owner_id, seq, f"memory_event_{uuid.uuid4().hex}", aggregate_type, aggregate_id, key, operation, actor, _now()),
        )

    @staticmethod
    def _entry(entry_id: str, owner_id: str, connection) -> MemoryEntry:
        row = connection.execute(
            "SELECT e.*,r.id revision_id,r.revision_no,r.content FROM memory_entries e LEFT JOIN memory_revisions r ON r.id=e.current_revision_id WHERE e.id=? AND e.owner_id=?",
            (entry_id, owner_id),
        ).fetchone()
        if not row: raise KeyError(entry_id)
        return MemoryEntry(row["id"], row["owner_id"], row["kind"], row["scope_type"], row["scope_id"], row["status"], row["content"] or "", row["revision_id"] or "", int(row["revision_no"] or 0), bool(row["pinned"]), float(row["importance"]), row["sensitivity"], row["created_at"], row["updated_at"])

    @staticmethod
    def _proposal(proposal_id: str, owner_id: str, connection) -> MemoryProposal:
        row = connection.execute("SELECT * FROM memory_proposals WHERE id=? AND owner_id=?", (proposal_id, owner_id)).fetchone()
        if not row: raise KeyError(proposal_id)
        return MemoryProposal(row["id"], row["owner_id"], row["operation"], row["target_entry_id"], row["base_revision_id"], row["kind"], row["scope_type"], row["scope_id"], row["content"], float(row["confidence"]), row["status"], row["accepted_revision_id"], row["reason"], row["created_at"])

    @staticmethod
    def _episode(episode_id: str, owner_id: str, connection) -> MemoryEpisode:
        row = connection.execute("SELECT * FROM memory_episodes WHERE id=? AND owner_id=?", (episode_id, owner_id)).fetchone()
        if not row: raise KeyError(episode_id)
        return MemoryEpisode(row["id"], row["owner_id"], row["thread_id"], row["project_id"], int(row["start_message_seq"]), int(row["end_message_seq"]), row["summary"], row["sensitivity"], row["retrieval_policy"], row["status"], row["created_at"])


class MemoryContextProvider:
    def __init__(self, db: Database) -> None: self.db = db

    def select(self, request: MemoryContextRequest) -> MemoryContextBundle:
        if request.model_invocation_id:
            prior = self._pin(request.model_invocation_id)
            if prior: return prior
        query_terms = {term.lower() for term in re.findall(r"[\w\u4e00-\u9fff]{2,}", request.query)}
        with self.db.connection() as connection:
            matched: set[str] = set()
            if request.query.strip():
                try:
                    matched = {row[0] for row in connection.execute("SELECT entry_id FROM memory_fts WHERE owner_id=? AND memory_fts MATCH ? LIMIT 50", (request.owner_id, request.query.strip()))}
                except Exception:
                    matched = set()
            entries = connection.execute(
                "SELECT e.*,r.id revision_id,r.content FROM memory_entries e JOIN memory_revisions r ON r.id=e.current_revision_id "
                "WHERE e.owner_id=? AND e.status='ACTIVE' AND (e.scope_type='user' OR (e.scope_type='project' AND e.scope_id=?))",
                (request.owner_id, request.project_id or ""),
            ).fetchall()
            episodes = connection.execute(
                "SELECT * FROM memory_episodes WHERE owner_id=? AND status IN ('ACTIVE','RAW_REFERENCE') AND "
                "(thread_id=? OR (project_id IS NOT NULL AND project_id=? AND retrieval_policy='project'))",
                (request.owner_id, request.thread_id, request.project_id),
            ).fetchall()
        def score(row) -> tuple[Any, ...]:
            content = row["content"].lower()
            match = sum(term in content for term in query_terms)
            return (-int(row["pinned"]), -(2 if row["id"] in matched else match), 0 if row["scope_type"] == "project" else 1, -float(row["importance"]), row["id"])
        entries = sorted(entries, key=score)
        chosen_entries, chosen_episodes, lines, reasons = [], [], [], []
        semantic = 0
        for row in entries:
            rendered = f"- [{row['kind']}/{row['scope_type']}] {row['content']}"
            cost = _tokens(rendered)
            if semantic + cost <= request.semantic_token_budget:
                lines.append(rendered); chosen_entries.append(row["revision_id"]); reasons.append("pinned" if row["pinned"] else "scope/query")
                semantic += cost
        episodic = 0
        for row in sorted(episodes, key=lambda item: (item["thread_id"] != request.thread_id, item["created_at"], item["id"]), reverse=False):
            rendered = f"- [episode] {row['summary']}"
            cost = _tokens(rendered)
            if episodic + cost <= request.episode_token_budget:
                lines.append(rendered); chosen_episodes.append(row["id"]); reasons.append("thread" if row["thread_id"] == request.thread_id else "project")
                episodic += cost
        rendered = "\n".join(lines)
        bundle = MemoryContextBundle(rendered, tuple(chosen_entries), tuple(chosen_episodes), tuple(reasons), len(entries) + len(episodes) - len(lines), _tokens(rendered), "chars-v1", "memory-v2", _hash(rendered))
        if request.model_invocation_id:
            self._save_pin(request, bundle)
        return bundle

    def _pin(self, invocation_id: str) -> MemoryContextBundle | None:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM memory_context_pins WHERE model_invocation_id=? AND invalidated_at IS NULL", (invocation_id,)).fetchone()
            if not row: return None
            revisions = json.loads(row["revision_ids_json"]); episodes = json.loads(row["episode_ids_json"])
            contents = []
            for rid in revisions:
                revision = connection.execute("SELECT content FROM memory_revisions WHERE id=?", (rid,)).fetchone()
                if revision: contents.append(revision["content"])
            for eid in episodes:
                episode = connection.execute("SELECT summary FROM memory_episodes WHERE id=?", (eid,)).fetchone()
                if episode: contents.append(episode["summary"])
        # Pin persistence stores identity/hash; re-select must return exact rendered text, so keep it in budget metadata.
        metadata = json.loads(row["budget_json"])
        rendered = metadata.get("rendered", "")
        return MemoryContextBundle(rendered, tuple(revisions), tuple(episodes), tuple(metadata.get("reasons", [])), int(metadata.get("dropped", 0)), int(row["token_count"]), row["tokenizer_version"], row["renderer_version"], row["rendered_hash"])

    def _save_pin(self, request: MemoryContextRequest, bundle: MemoryContextBundle) -> None:
        now = _now(); scope = json.dumps({"project_id": request.project_id, "thread_id": request.thread_id}, sort_keys=True)
        metadata = {"semantic": request.semantic_token_budget, "episodic": request.episode_token_budget, "rendered": bundle.rendered, "reasons": bundle.reasons, "dropped": bundle.dropped}
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO memory_context_pins(model_invocation_id,owner_id,parent_type,parent_id,purpose,query_hash,scope_hash,revision_ids_json,episode_ids_json,tokenizer_version,renderer_version,budget_json,token_count,rendered_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request.model_invocation_id, request.owner_id, request.parent_type, request.parent_id or request.thread_id, request.purpose, _hash(request.query), _hash(scope), json.dumps(bundle.revision_ids), json.dumps(bundle.episode_ids), bundle.tokenizer_version, bundle.renderer_version, json.dumps(metadata, ensure_ascii=False), bundle.token_count, bundle.bundle_hash, now),
            )


def _revision(row) -> MemoryRevision:
    return MemoryRevision(row["id"], row["entry_id"], int(row["revision_no"]), row["operation"], row["content"], row["base_revision_id"], row["actor"], tuple(json.loads(row["source_refs_json"])), row["reason"], row["created_at"])


def _validate_kind_scope(kind: str, scope_type: str, scope_id: str) -> None:
    if kind not in KINDS or scope_type not in SCOPES: raise ValueError("invalid memory kind or scope")
    if scope_type == "project" and not scope_id: raise ValueError("project memory requires scope_id")
    if scope_type == "user" and scope_id: raise ValueError("user memory cannot have scope_id")


def _validate_content(content: str) -> str:
    content = content.strip()
    if not content: raise ValueError("memory content is required")
    if SECRET_RE.search(content): raise ValueError("secret-like content cannot be stored as memory")
    return content


def _redact(text: str) -> str: return SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
def _fingerprint(text: str) -> str: return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()
def _hash(text: str) -> str: return "sha256:" + hashlib.sha256(text.encode()).hexdigest()
def _tokens(text: str) -> int: return (len(text) + 3) // 4
def _safe_id(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()[:24]
def _now() -> str: return datetime.now(timezone.utc).isoformat()
