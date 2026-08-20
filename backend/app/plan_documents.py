from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .db import Database


MAX_PLAN_MARKDOWN_BYTES = 1024 * 1024
MAX_PLAN_TITLE_LENGTH = 120
PlanDocumentFileStatus = Literal["pending", "ready", "conflict", "failed"]
PlanDocumentVersionStatus = Literal["prepared", "committed", "abandoned"]
PlanWriteIntentStatus = Literal["PREPARED", "FILE_WRITTEN", "COMMITTED", "CONFLICT", "FAILED"]


class PlanDocumentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PlanDocument:
    id: str
    thread_id: str
    title: str
    current_version_id: str | None
    projected_version_id: str | None
    file_status: PlanDocumentFileStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlanDocumentVersion:
    id: str
    plan_document_id: str
    version: int
    base_version_id: str | None
    title: str
    markdown_content: str
    content_hash: str
    source_turn_id: str | None
    source_message_id: str | None
    actor: str
    change_summary: str
    status: PlanDocumentVersionStatus
    created_at: str
    committed_at: str | None


@dataclass(frozen=True)
class PlanWriteIntent:
    id: str
    plan_document_id: str
    version_id: str
    expected_head_version_id: str | None
    expected_file_hash: str | None
    target_file_hash: str
    status: PlanWriteIntentStatus
    attempts: int
    last_error_json: str | None
    created_at: str
    finished_at: str | None


def normalize_markdown(content: str) -> str:
    if not isinstance(content, str):
        raise PlanDocumentValidationError("markdown content must be a string")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    if "\x00" in normalized:
        raise PlanDocumentValidationError("markdown content cannot contain NUL")
    validate_document_content(normalized)
    return normalized


def validate_document_content(content: str) -> str:
    if not isinstance(content, str):
        raise PlanDocumentValidationError("markdown content must be a string")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PlanDocumentValidationError("markdown content must be valid UTF-8") from exc
    if len(encoded) > MAX_PLAN_MARKDOWN_BYTES:
        raise PlanDocumentValidationError("markdown content exceeds 1 MiB")
    if "\x00" in content:
        raise PlanDocumentValidationError("markdown content cannot contain NUL")
    if not content.strip():
        raise PlanDocumentValidationError("markdown content cannot be empty")
    return content


def validate_title(title: str) -> str:
    if not isinstance(title, str):
        raise PlanDocumentValidationError("plan title must be a string")
    if not title or not title.strip() or len(title) > MAX_PLAN_TITLE_LENGTH:
        raise PlanDocumentValidationError("plan title length must be between 1 and 120")
    if "\x00" in title:
        raise PlanDocumentValidationError("plan title cannot contain NUL")
    return title


def content_hash(content: str) -> str:
    normalized = normalize_markdown(content)
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class PlanDocumentConflict(ValueError):
    pass


class PlanDocumentService:
    def __init__(self, db: Database, data_root: str | Path, *, projector=None, events=None) -> None:
        from .plan_files import PlanFileProjector

        self.db = db
        self.projector = projector or PlanFileProjector(data_root)
        self.events = events

    def path_for(self, document_id: str) -> Path:
        return self.projector.path_for(document_id)

    def get_by_thread(self, thread_id: str) -> PlanDocument:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM plan_documents WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if row is None:
            raise KeyError(thread_id)
        return _document_from_row(row)

    def current_version(self, document_id: str) -> PlanDocumentVersion:
        with self.db.connection() as connection:
            document = connection.execute(
                "SELECT current_version_id FROM plan_documents WHERE id = ?", (document_id,)
            ).fetchone()
        if document is None or document["current_version_id"] is None:
            raise KeyError(document_id)
        return self.get_version(document["current_version_id"])

    def get_version(self, version_id: str) -> PlanDocumentVersion:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM plan_document_versions WHERE id = ?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
        return _version_from_row(row)

    def list_versions(self, document_id: str) -> list[PlanDocumentVersion]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_document_versions WHERE plan_document_id = ? ORDER BY version",
                (document_id,),
            ).fetchall()
        return [_version_from_row(row) for row in rows]

    def pending_intents(self, document_id: str) -> list[PlanWriteIntent]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_write_intents WHERE plan_document_id = ? "
                "AND status NOT IN ('COMMITTED', 'CONFLICT') ORDER BY created_at",
                (document_id,),
            ).fetchall()
        return [_intent_from_row(row) for row in rows]

    def save_model_revision(
        self,
        *,
        thread_id: str,
        title: str,
        markdown_content: str,
        source_turn_id: str | None,
        source_message_id: str | None,
        actor: str,
        expected_version_id: str | None = None,
        expected_file_hash: str | None = None,
        change_summary: str = "",
    ) -> PlanDocumentVersion:
        title = validate_title(title)
        markdown_content = normalize_markdown(markdown_content)
        target_hash = content_hash(markdown_content)
        if source_turn_id:
            with self.db.connection() as connection:
                existing = connection.execute(
                    "SELECT id, status FROM plan_document_versions WHERE source_turn_id = ?",
                    (source_turn_id,),
                ).fetchone()
            if existing is not None:
                if existing["status"] == "committed":
                    return self.get_version(existing["id"])
                self.recover_pending_intents()
                recovered = self.get_version(existing["id"])
                if recovered.status == "committed":
                    return recovered
                raise PlanDocumentConflict("existing plan document revision is not committed")
        with self.db.durable_transaction() as connection:
            if source_turn_id:
                existing = connection.execute(
                    "SELECT * FROM plan_document_versions WHERE source_turn_id = ?",
                    (source_turn_id,),
                ).fetchone()
                if existing is not None:
                    if existing["status"] == "committed":
                        return _version_from_row(existing)
                    raise PlanDocumentConflict("existing plan document revision is not committed")
            document = connection.execute(
                "SELECT * FROM plan_documents WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            now = _now()
            if document is None:
                document_id = f"plan_{uuid.uuid4().hex}"
                connection.execute(
                    "INSERT INTO plan_documents(id, thread_id, title, file_status, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'pending', ?, ?)",
                    (document_id, thread_id, title, now, now),
                )
                document = connection.execute(
                    "SELECT * FROM plan_documents WHERE id = ?", (document_id,)
                ).fetchone()
            current_id = document["current_version_id"]
            if current_id != expected_version_id:
                if current_id is not None or expected_version_id is not None:
                    raise PlanDocumentConflict("plan document head conflict")
            if expected_file_hash is not None:
                current_file_hash = self.projector.read_hash(document["id"])
                if current_file_hash != expected_file_hash:
                    raise PlanDocumentConflict("plan file hash conflict")
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM plan_document_versions WHERE plan_document_id = ?",
                (document["id"],),
            ).fetchone()[0] + 1
            version_id = f"planv_{uuid.uuid4().hex}"
            intent_id = f"intent_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO plan_document_versions("
                "id, plan_document_id, version, base_version_id, title, markdown_content, content_hash, "
                "source_turn_id, source_message_id, actor, change_summary, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?)",
                (
                    version_id,
                    document["id"],
                    version,
                    current_id,
                    title,
                    markdown_content,
                    target_hash,
                    source_turn_id,
                    source_message_id,
                    actor,
                    change_summary,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO plan_write_intents("
                "id, plan_document_id, version_id, expected_head_version_id, expected_file_hash, "
                "target_file_hash, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'PREPARED', ?)",
                (intent_id, document["id"], version_id, current_id, expected_file_hash, target_hash, now),
            )
            prepared = _version_from_row(
                connection.execute(
                    "SELECT * FROM plan_document_versions WHERE id = ?", (version_id,)
                ).fetchone()
            )
            self._append_event(
                connection,
                prepared,
                "plan.document_prepared",
                "model" if actor == "model" else actor,
                now,
            )
        try:
            self.projector.project(
                prepared.plan_document_id,
                prepared.markdown_content,
                expected_file_hash=expected_file_hash,
            )
        except BaseException as exc:
            with self.db.durable_transaction() as connection:
                connection.execute(
                    "UPDATE plan_write_intents SET status = 'FAILED', attempts = attempts + 1, "
                    "last_error_json = ?, finished_at = ? WHERE version_id = ?",
                    (json.dumps({"error": str(exc)}, ensure_ascii=False), _now(), prepared.id),
                )
                connection.execute(
                    "UPDATE plan_documents SET file_status = 'failed', updated_at = ? WHERE id = ?",
                    (_now(), prepared.plan_document_id),
                )
                self._append_event(
                    connection,
                    prepared,
                    "plan.document_failed",
                    "worker",
                    _now(),
                    {"reason": str(exc)[:240]},
                )
            raise
        with self.db.durable_transaction() as connection:
            document = connection.execute(
                "SELECT * FROM plan_documents WHERE id = ?", (prepared.plan_document_id,)
            ).fetchone()
            if document["current_version_id"] != prepared.base_version_id:
                connection.execute(
                    "UPDATE plan_write_intents SET status = 'CONFLICT', finished_at = ? WHERE version_id = ?",
                    (_now(), prepared.id),
                )
                connection.execute(
                    "UPDATE plan_documents SET file_status = 'conflict', updated_at = ? WHERE id = ?",
                    (_now(), prepared.plan_document_id),
                )
                self._append_event(
                    connection,
                    prepared,
                    "plan.document_conflict",
                    "worker",
                    _now(),
                    {"reason": "plan document head conflict"},
                )
                raise PlanDocumentConflict("plan document head conflict")
            now = _now()
            connection.execute(
                "UPDATE plan_document_versions SET status = 'committed', committed_at = ? WHERE id = ?",
                (now, prepared.id),
            )
            connection.execute(
                "UPDATE plan_documents SET title = ?, current_version_id = ?, projected_version_id = ?, "
                "file_status = 'ready', updated_at = ? WHERE id = ?",
                (prepared.title, prepared.id, prepared.id, now, prepared.plan_document_id),
            )
            connection.execute(
                "UPDATE plan_write_intents SET status = 'COMMITTED', attempts = attempts + 1, finished_at = ? "
                "WHERE version_id = ?",
                (now, prepared.id),
            )
            self._append_event(connection, prepared, "plan.document_version_created", actor, now)
            self._append_event(connection, prepared, "plan.document_ready", "worker", now)
        return self.get_version(prepared.id)

    def _append_event(
        self,
        connection,
        version: PlanDocumentVersion,
        event_type: str,
        actor: str,
        occurred_at: str,
        extra: dict | None = None,
    ) -> None:
        if self.events is None:
            return
        row = connection.execute(
            "SELECT thread_id FROM plan_documents WHERE id = ?", (version.plan_document_id,)
        ).fetchone()
        if row is None:
            return
        data = {
            "plan_document_id": version.plan_document_id,
            "version_id": version.id,
            "version": version.version,
            "content_hash": version.content_hash,
            "source_message_id": version.source_message_id,
            "actor": version.actor,
        }
        if extra:
            data.update(extra)
        turn_id = version.source_turn_id or f"plan_{version.id}"
        self.events.append(
            row["thread_id"],
            turn_id,
            event_type,
            actor,
            data,
            connection=connection,
            occurred_at=occurred_at,
        )

    def restore_version(
        self,
        document_id: str,
        version: int,
        *,
        actor: str = "restore",
        expected_version: int | None = None,
        expected_file_hash: str | None = None,
    ) -> PlanDocumentVersion:
        source = next(item for item in self.list_versions(document_id) if item.version == version)
        document = self.get_document(document_id)
        current = self.current_version(document_id)
        if expected_version is not None and current.version != expected_version:
            raise PlanDocumentConflict("plan document head conflict")
        if expected_file_hash is not None and current.content_hash != expected_file_hash:
            raise PlanDocumentConflict("plan document hash conflict")
        return self.save_model_revision(
            thread_id=document.thread_id,
            title=source.title,
            markdown_content=source.markdown_content,
            source_turn_id=None,
            source_message_id=None,
            actor=actor,
            expected_version_id=current.id,
            expected_file_hash=current.content_hash,
            change_summary=f"restore v{version}",
        )

    def retry_projection(self, document_id: str) -> PlanDocument:
        self.get_document(document_id)
        self.recover_pending_intents()
        return self.get_document(document_id)

    def sync_file(self, document_id: str) -> PlanDocumentVersion:
        """Import a safe third-party edit as a new immutable revision."""
        from .plan_files import PlanFileConflict

        document = self.get_document(document_id)
        current = self.current_version(document_id)
        path = self.path_for(document_id)
        if not path.exists():
            self.projector.project(document_id, current.markdown_content, expected_file_hash=None)
            self._mark_document_ready(document_id, current.id)
            return current
        try:
            reader = getattr(self.projector, "read_text_stable", None)
            raw_content = reader(document_id) if reader is not None else path.read_text(encoding="utf-8")
            content = normalize_markdown(raw_content)
        except (OSError, UnicodeError, PlanDocumentValidationError, PlanFileConflict, ValueError) as exc:
            self._mark_document_failed(document_id, str(exc))
            message = "plan file changed while reading" if isinstance(exc, PlanFileConflict) else "plan file cannot be imported"
            raise PlanDocumentValidationError(message) from exc
        file_hash = content_hash(content)
        if file_hash == current.content_hash:
            self._mark_document_ready(document_id, current.id)
            return current
        if document.projected_version_id != current.id:
            self._mark_document_conflict(document_id, "plan file and database head conflict")
            raise PlanDocumentConflict("plan file and database head conflict")
        return self.save_model_revision(
            thread_id=document.thread_id,
            title=document.title,
            markdown_content=content,
            source_turn_id=None,
            source_message_id=None,
            actor="filesystem",
            expected_version_id=current.id,
            expected_file_hash=file_hash,
            change_summary="import external plan.md edit",
        )

    def recover_pending_intents(self) -> None:
        """Recover durable document writes without calling the model again.

        A prepared revision contains the complete, validated Markdown, so the
        recovery path only compares the file hash and resumes the existing
        PREPARE/PROJECT/FINALIZE protocol.  A hash that is neither the
        expected hash nor the target hash means that somebody else changed the
        file; that file is preserved and the document is marked conflicted.
        """
        with self.db.connection() as connection:
            intent_rows = connection.execute(
                "SELECT * FROM plan_write_intents "
                "WHERE status NOT IN ('COMMITTED', 'CONFLICT') ORDER BY created_at"
            ).fetchall()

        for row in intent_rows:
            self._recover_intent(_intent_from_row(row))

        self._rebuild_missing_committed_files()

    def _recover_intent(self, intent: PlanWriteIntent) -> None:
        try:
            document = self.get_document(intent.plan_document_id)
            version = self.get_version(intent.version_id)
        except KeyError:
            self._mark_intent_conflict(intent.id, "plan document or revision is missing")
            return

        if version.status != "prepared":
            if version.status == "committed":
                self._mark_intent_committed(intent.id)
            else:
                self._mark_intent_conflict(intent.id, "revision is not recoverable")
            return

        try:
            current_file_hash = self.projector.read_hash(document.id)
        except (OSError, UnicodeError, ValueError) as exc:
            self._mark_intent_conflict(intent.id, f"unable to inspect plan file: {exc}")
            return

        if current_file_hash == intent.target_file_hash:
            self._finalize_prepared(intent.id)
            return

        if current_file_hash != intent.expected_file_hash:
            self._mark_intent_conflict(intent.id, "plan file changed during recovery")
            return

        try:
            self.projector.project(
                document.id,
                version.markdown_content,
                expected_file_hash=intent.expected_file_hash,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            self._mark_intent_failed(intent.id, exc)
            return

        with self.db.durable_transaction() as connection:
            connection.execute(
                "UPDATE plan_write_intents SET status = 'FILE_WRITTEN', attempts = attempts + 1, "
                "last_error_json = NULL WHERE id = ? AND status NOT IN ('COMMITTED', 'CONFLICT')",
                (intent.id,),
            )
        self._finalize_prepared(intent.id)

    def _finalize_prepared(self, intent_id: str) -> None:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM plan_write_intents WHERE id = ?", (intent_id,)
            ).fetchone()
        if row is None or row["status"] in {"COMMITTED", "CONFLICT"}:
            return

        intent = _intent_from_row(row)
        try:
            document = self.get_document(intent.plan_document_id)
            version = self.get_version(intent.version_id)
            current_file_hash = self.projector.read_hash(document.id)
        except (KeyError, OSError, UnicodeError, ValueError) as exc:
            self._mark_intent_conflict(intent.id, f"unable to finalize plan file: {exc}")
            return

        if version.status != "prepared" or current_file_hash != intent.target_file_hash:
            if version.status == "committed":
                self._mark_intent_committed(intent.id)
            else:
                self._mark_intent_conflict(intent.id, "plan file is not at the target hash")
            return

        with self.db.durable_transaction() as connection:
            current = connection.execute(
                "SELECT * FROM plan_documents WHERE id = ?", (document.id,)
            ).fetchone()
            if current is None or current["current_version_id"] != intent.expected_head_version_id:
                connection.execute(
                    "UPDATE plan_write_intents SET status = 'CONFLICT', finished_at = ? WHERE id = ?",
                    (_now(), intent.id),
                )
                return

            now = _now()
            connection.execute(
                "UPDATE plan_document_versions SET status = 'committed', committed_at = ? "
                "WHERE id = ? AND status = 'prepared'",
                (now, version.id),
            )
            connection.execute(
                "UPDATE plan_documents SET title = ?, current_version_id = ?, projected_version_id = ?, "
                "file_status = 'ready', updated_at = ? WHERE id = ?",
                (version.title, version.id, version.id, now, document.id),
            )
            connection.execute(
                "UPDATE plan_write_intents SET status = 'COMMITTED', attempts = attempts + 1, "
                "last_error_json = NULL, finished_at = ? WHERE id = ?",
                (now, intent.id),
            )
            if version.source_message_id:
                connection.execute(
                    "UPDATE thread_messages SET plan_document_version_id = ? WHERE id = ?",
                    (version.id, version.source_message_id),
                )
            self._append_event(connection, version, "plan.document_version_created", version.actor, now)
            self._append_event(connection, version, "plan.document_ready", "worker", now)

    def _rebuild_missing_committed_files(self) -> None:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_documents WHERE current_version_id IS NOT NULL"
            ).fetchall()

        for row in rows:
            document = _document_from_row(row)
            try:
                version = self.get_version(document.current_version_id)
                current_file_hash = self.projector.read_hash(document.id)
            except (KeyError, OSError, UnicodeError, ValueError) as exc:
                self._mark_document_conflict(document.id, f"unable to inspect committed plan file: {exc}")
                continue

            if current_file_hash == version.content_hash:
                self._mark_document_ready(document.id, version.id)
                continue
            if current_file_hash is not None:
                self._mark_document_conflict(document.id, "committed plan file has an external edit")
                continue

            try:
                self.projector.project(document.id, version.markdown_content, expected_file_hash=None)
            except (OSError, UnicodeError, ValueError) as exc:
                self._mark_document_failed(document.id, f"unable to rebuild committed plan file: {exc}")
                continue
            self._mark_document_ready(document.id, version.id)

    def _mark_intent_failed(self, intent_id: str, error: BaseException) -> None:
        with self.db.durable_transaction() as connection:
            connection.execute(
                "UPDATE plan_write_intents SET status = 'FAILED', attempts = attempts + 1, "
                "last_error_json = ?, finished_at = ? WHERE id = ?",
                (json.dumps({"error": str(error)}, ensure_ascii=False), _now(), intent_id),
            )

    def _mark_intent_conflict(self, intent_id: str, message: str) -> None:
        with self.db.durable_transaction() as connection:
            row = connection.execute(
                "SELECT plan_document_id, version_id FROM plan_write_intents WHERE id = ?", (intent_id,)
            ).fetchone()
            document_row = None
            if row is not None:
                document_row = connection.execute(
                    "SELECT file_status FROM plan_documents WHERE id = ?",
                    (row["plan_document_id"],),
                ).fetchone()
            connection.execute(
                "UPDATE plan_write_intents SET status = 'CONFLICT', attempts = attempts + 1, "
                "last_error_json = ?, finished_at = ? WHERE id = ?",
                (json.dumps({"error": message}, ensure_ascii=False), _now(), intent_id),
            )
            if row is not None:
                connection.execute(
                    "UPDATE plan_documents SET file_status = 'conflict', updated_at = ? WHERE id = ?",
                    (_now(), row["plan_document_id"]),
                )
                if self.events is not None and (document_row is None or document_row["file_status"] != "conflict"):
                    version_row = connection.execute(
                        "SELECT * FROM plan_document_versions WHERE id = ?",
                        (row["version_id"],),
                    ).fetchone()
                    if version_row is not None:
                        self._append_event(
                            connection,
                            _version_from_row(version_row),
                            "plan.document_conflict",
                            "worker",
                            _now(),
                            {"reason": message[:240]},
                        )

    def _mark_intent_committed(self, intent_id: str) -> None:
        with self.db.durable_transaction() as connection:
            connection.execute(
                "UPDATE plan_write_intents SET status = 'COMMITTED', finished_at = ? WHERE id = ?",
                (_now(), intent_id),
            )

    def _mark_document_ready(self, document_id: str, version_id: str) -> None:
        with self.db.durable_transaction() as connection:
            connection.execute(
                "UPDATE plan_documents SET projected_version_id = ?, file_status = 'ready', updated_at = ? "
                "WHERE id = ?",
                (version_id, _now(), document_id),
            )

    def _mark_document_conflict(self, document_id: str, message: str) -> None:
        with self.db.durable_transaction() as connection:
            row = connection.execute(
                "SELECT file_status, current_version_id FROM plan_documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                return
            should_emit = row["file_status"] != "conflict"
            connection.execute(
                "UPDATE plan_documents SET file_status = 'conflict', updated_at = ? WHERE id = ?",
                (_now(), document_id),
            )
            if should_emit and self.events is not None and row["current_version_id"]:
                version_row = connection.execute(
                    "SELECT * FROM plan_document_versions WHERE id = ?",
                    (row["current_version_id"],),
                ).fetchone()
                if version_row is not None:
                    version = _version_from_row(version_row)
                    self._append_event(
                        connection,
                        version,
                        "plan.document_conflict",
                        "worker",
                        _now(),
                        {"reason": message[:240]},
                    )

    def _mark_document_failed(self, document_id: str, message: str) -> None:
        with self.db.durable_transaction() as connection:
            row = connection.execute(
                "SELECT file_status, current_version_id FROM plan_documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                return
            should_emit = row["file_status"] != "failed"
            connection.execute(
                "UPDATE plan_documents SET file_status = 'failed', updated_at = ? WHERE id = ?",
                (_now(), document_id),
            )
            if should_emit and self.events is not None and row["current_version_id"]:
                version_row = connection.execute(
                    "SELECT * FROM plan_document_versions WHERE id = ?",
                    (row["current_version_id"],),
                ).fetchone()
                if version_row is not None:
                    version = _version_from_row(version_row)
                    self._append_event(
                        connection,
                        version,
                        "plan.document_failed",
                        "worker",
                        _now(),
                        {"reason": message[:240]},
                    )

    def get_document(self, document_id: str) -> PlanDocument:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM plan_documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            raise KeyError(document_id)
        return _document_from_row(row)


def _document_from_row(row) -> PlanDocument:
    return PlanDocument(
        id=row["id"], thread_id=row["thread_id"], title=row["title"],
        current_version_id=row["current_version_id"], projected_version_id=row["projected_version_id"],
        file_status=row["file_status"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _version_from_row(row) -> PlanDocumentVersion:
    return PlanDocumentVersion(
        id=row["id"], plan_document_id=row["plan_document_id"], version=row["version"],
        base_version_id=row["base_version_id"], title=row["title"], markdown_content=row["markdown_content"],
        content_hash=row["content_hash"], source_turn_id=row["source_turn_id"],
        source_message_id=row["source_message_id"], actor=row["actor"],
        change_summary=row["change_summary"], status=row["status"], created_at=row["created_at"],
        committed_at=row["committed_at"],
    )


def _intent_from_row(row) -> PlanWriteIntent:
    return PlanWriteIntent(
        id=row["id"], plan_document_id=row["plan_document_id"], version_id=row["version_id"],
        expected_head_version_id=row["expected_head_version_id"], expected_file_hash=row["expected_file_hash"],
        target_file_hash=row["target_file_hash"], status=row["status"], attempts=row["attempts"],
        last_error_json=row["last_error_json"], created_at=row["created_at"], finished_at=row["finished_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
