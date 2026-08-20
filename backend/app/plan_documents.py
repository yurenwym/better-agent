from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal


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
    return content


def validate_title(title: str) -> str:
    if not isinstance(title, str):
        raise PlanDocumentValidationError("plan title must be a string")
    if not title or len(title) > MAX_PLAN_TITLE_LENGTH:
        raise PlanDocumentValidationError("plan title length must be between 1 and 120")
    if "\x00" in title:
        raise PlanDocumentValidationError("plan title cannot contain NUL")
    return title


def content_hash(content: str) -> str:
    normalized = normalize_markdown(content)
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
