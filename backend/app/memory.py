from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database
from .events import EventStore, content_hash


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    run_id: str | None
    kind: str
    content: str
    scope: str
    project_id: str | None
    skill_name: str | None
    confidence: float
    status: str
    version: int | None
    path: str


@dataclass(frozen=True)
class MemoryVersion:
    path: str
    version: int
    content: str
    content_hash: str


class MemoryService:
    def __init__(self, db: Database, events: EventStore, root: str | Path) -> None:
        self.db = db
        self.events = events
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".versions").mkdir(parents=True, exist_ok=True)

    def create_candidate(
        self,
        run_id: str,
        goal_id: str,
        kind: str,
        content: str,
        scope: str,
        confidence: float,
        evidence_event_ids: list[str],
        project_id: str | None = None,
        skill_name: str | None = None,
    ) -> MemoryRecord:
        if kind not in {"preference", "habit"} or scope not in {"global", "project", "skill"}:
            raise ValueError("invalid memory kind or scope")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        memory_id = f"memory_{uuid.uuid4().hex}"
        now = _now()
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_candidates(id, run_id, kind, content, scope, project_id, skill_name, confidence, "
                "evidence_event_ids_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
                (
                    memory_id,
                    run_id,
                    kind,
                    content,
                    scope,
                    project_id,
                    skill_name,
                    confidence,
                    json.dumps(evidence_event_ids),
                    now,
                    now,
                ),
            )
        record = self.get(memory_id)
        self.events.append(
            run_id,
            goal_id,
            "memory.candidate_created",
            "runtime",
            {"memory_id": memory_id, "scope": scope, "kind": kind},
        )
        return record

    def get(self, memory_id: str) -> MemoryRecord:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM memory_candidates WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return self._record(row)

    def all_records(self) -> list[MemoryRecord]:
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM memory_candidates ORDER BY created_at, id").fetchall()
        return [self._record(row) for row in rows]

    def confirm(self, memory_id: str, content: str | None = None) -> MemoryRecord:
        record = self.get(memory_id)
        if record.status not in {"proposed", "confirmed"}:
            raise ValueError("only proposed or confirmed memory can be confirmed")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE memory_candidates SET status = 'confirmed', content = ?, updated_at = ? WHERE id = ?",
                (content if content is not None else record.content, _now(), memory_id),
            )
        updated = self.get(memory_id)
        self._rewrite_path(updated.path)
        self.events.append(
            updated.run_id or "memory",
            "memory",
            "memory.confirmed",
            "user",
            {"memory_id": memory_id, "path": updated.path},
        )
        return self.get(memory_id)

    def reject(self, memory_id: str) -> MemoryRecord:
        record = self.get(memory_id)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE memory_candidates SET status = 'rejected', updated_at = ? WHERE id = ?",
                (_now(), memory_id),
            )
        self.events.append(record.run_id or "memory", "memory", "memory.rejected", "user", {"memory_id": memory_id})
        return self.get(memory_id)

    def disable(self, memory_id: str) -> MemoryRecord:
        record = self.get(memory_id)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE memory_candidates SET status = 'disabled', updated_at = ? WHERE id = ?",
                (_now(), memory_id),
            )
        self._rewrite_path(record.path)
        return self.get(memory_id)

    def edit(self, memory_id: str, content: str) -> MemoryRecord:
        record = self.get(memory_id)
        if record.status != "confirmed":
            raise ValueError("only confirmed memory can be edited")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE memory_candidates SET content = ?, updated_at = ? WHERE id = ?",
                (content, _now(), memory_id),
            )
        updated = self.get(memory_id)
        self._rewrite_path(updated.path)
        return self.get(memory_id)

    def versions(self, memory_id: str) -> list[MemoryVersion]:
        record = self.get(memory_id)
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_file_versions WHERE path = ? ORDER BY version",
                (record.path,),
            ).fetchall()
        return [MemoryVersion(row["path"], row["version"], row["content"], row["content_hash"]) for row in rows]

    def rollback(self, memory_id: str, version: int) -> MemoryRecord:
        record = self.get(memory_id)
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT content FROM memory_file_versions WHERE path = ? AND version = ?",
                (record.path, version),
            ).fetchone()
        if row is None:
            raise KeyError(f"memory version {version}")
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE memory_candidates SET content = ?, status = 'confirmed', updated_at = ? WHERE id = ?",
                (row["content"].rstrip("\n"), _now(), memory_id),
            )
        updated = self.get(memory_id)
        self._rewrite_path(updated.path)
        return self.get(memory_id)

    def context_memories(self, project_id: str | None, skill_name: str | None) -> list[MemoryRecord]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_candidates WHERE status = 'confirmed' ORDER BY created_at, id"
            ).fetchall()
        records = [self._record(row) for row in rows]
        rank = {"project": 0, "skill": 1, "global": 2}
        return sorted(
            [
                record
                for record in records
                if record.scope == "global"
                or (record.scope == "project" and record.project_id == project_id)
                or (record.scope == "skill" and record.skill_name == skill_name)
            ],
            key=lambda record: (rank.get(record.scope, 9), record.id),
        )

    def apply_confirmed(
        self,
        run_id: str,
        goal_id: str,
        project_id: str | None,
        skill_name: str | None,
    ) -> list[MemoryRecord]:
        records = self.context_memories(project_id, skill_name)
        for record in records:
            self.events.append(
                run_id,
                goal_id,
                "memory.applied",
                "runtime",
                {"memory_id": record.id, "version": record.version, "scope": record.scope},
            )
        return records

    def sync_manual_edits(self) -> list[str]:
        changed: list[str] = []
        with self.db.connection() as connection:
            rows = connection.execute("SELECT DISTINCT path FROM memory_candidates WHERE status = 'confirmed'").fetchall()
        for row in rows:
            path = self.root / row["path"]
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8")
            with self.db.connection() as connection:
                latest = connection.execute(
                    "SELECT content_hash FROM memory_file_versions WHERE path = ? ORDER BY version DESC LIMIT 1",
                    (row["path"],),
                ).fetchone()
            if latest and latest["content_hash"] != content_hash(content):
                self._store_version(row["path"], content)
                changed.append(row["path"])
        return changed

    def _rewrite_path(self, relative_path: str) -> None:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT content FROM memory_candidates WHERE status = 'confirmed' AND "
                "((scope = 'global' AND ? = 'preferences.md') OR (scope = 'project' AND ? LIKE 'projects/%')) "
                "ORDER BY created_at, id",
                (relative_path, relative_path),
            ).fetchall()
        content = "".join(f"{row['content'].rstrip(chr(10))}\n" for row in rows)
        self._write_versioned(relative_path, content)
        with self.db.transaction() as connection:
            version = connection.execute(
                "SELECT version FROM memory_file_versions WHERE path = ? ORDER BY version DESC LIMIT 1",
                (relative_path,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE memory_candidates SET memory_version = ? WHERE status = 'confirmed' AND "
                "((scope = 'global' AND ? = 'preferences.md') OR (scope = 'project' AND ? LIKE 'projects/%'))",
                (version, relative_path, relative_path),
            )

    def _write_versioned(self, relative_path: str, content: str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            current = path.read_text(encoding="utf-8")
            with self.db.connection() as connection:
                latest = connection.execute(
                    "SELECT content_hash FROM memory_file_versions WHERE path = ? ORDER BY version DESC LIMIT 1",
                    (relative_path,),
                ).fetchone()
            if latest is None or latest["content_hash"] != content_hash(current):
                self._store_version(relative_path, current)
        self._store_version(relative_path, content)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".memory-", delete=False) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)

    def _store_version(self, relative_path: str, content: str) -> int:
        with self.db.transaction() as connection:
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM memory_file_versions WHERE path = ?",
                (relative_path,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO memory_file_versions(id, path, version, content_hash, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"memory-file-{uuid.uuid4().hex}", relative_path, version, content_hash(content), content, _now()),
            )
        return version

    def _record(self, row: Any) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            run_id=row["run_id"],
            kind=row["kind"],
            content=row["content"],
            scope=row["scope"],
            project_id=row["project_id"],
            skill_name=row["skill_name"],
            confidence=row["confidence"],
            status=row["status"],
            version=row["memory_version"],
            path=self._path_for(row["scope"], row["project_id"], row["skill_name"]),
        )

    @staticmethod
    def _path_for(scope: str, project_id: str | None, skill_name: str | None) -> str:
        if scope == "project":
            return f"projects/{project_id}.md"
        if scope == "skill":
            return f"skills/{skill_name}.md"
        return "preferences.md"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
