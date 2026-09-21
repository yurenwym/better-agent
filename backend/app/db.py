from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


POSTGRES_SCHEMA_HEAD = "20260920_0020"
POSTGRES_REQUIRED_EXTENSIONS = frozenset({"vector", "pg_trgm"})


def _postgres_schema_is_current(version: str | None, extensions: set[str]) -> bool:
    return version == POSTGRES_SCHEMA_HEAD and POSTGRES_REQUIRED_EXTENSIONS <= extensions


def _is_postgres_url(value: str | Path) -> bool:
    return str(value).startswith(("postgresql://", "postgres://"))


def _postgres_sql(sql: str) -> str:
    """Translate qmark binds without touching quoted SQL text or comments."""
    result: list[str] = []
    index = 0
    quote: str | None = None
    dollar_quote: str | None = None
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if dollar_quote:
            if sql.startswith(dollar_quote, index):
                result.append(dollar_quote)
                index += len(dollar_quote)
                dollar_quote = None
                continue
            result.append(char)
        elif quote:
            result.append(char)
            if char == quote:
                if following == quote:
                    result.append(following)
                    index += 1
                else:
                    quote = None
        elif char == "$":
            match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[index:])
            if match:
                dollar_quote = match.group(0)
                result.append(dollar_quote)
                index += len(dollar_quote)
                continue
            result.append(char)
        elif char in {"'", '"'}:
            quote = char
            result.append(char)
        elif char == "-" and following == "-":
            end = sql.find("\n", index)
            if end < 0:
                result.append(sql[index:])
                break
            result.append(sql[index:end])
            index = end - 1
        elif char == "/" and following == "*":
            end = sql.find("*/", index + 2)
            if end < 0:
                result.append(sql[index:])
                break
            result.append(sql[index : end + 2])
            index = end + 1
        elif char == "?":
            previous_nonspace = next(
                (candidate for candidate in reversed(result) if candidate and not candidate[-1].isspace()),
                "",
            )
            remaining = sql[index + 1:].lstrip()
            json_operator = following in {"|", "&"} or (
                remaining.startswith(("'", '"'))
                and previous_nonspace
                and previous_nonspace[-1] not in "=<>!,( ["
            )
            if json_operator:
                result.append(char)
            else:
                result.append("%s")
                if following and (following.isalnum() or following == "_"):
                    result.append(" ")
        else:
            result.append(char)
        index += 1
    converted = "".join(result)
    if converted.lstrip().upper().startswith("INSERT OR IGNORE INTO "):
        converted = converted.replace("INSERT OR IGNORE INTO ", "INSERT INTO ", 1)
        converted = converted.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return converted


class _CompatRow(tuple):
    def __new__(cls, values: tuple[Any, ...], names: tuple[str, ...]):
        instance = super().__new__(cls, values)
        instance._names = names
        instance._positions = {name: offset for offset, name in enumerate(names)}
        return instance

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._positions[key]
        return super().__getitem__(key)

    def keys(self) -> list[str]:
        return list(self._names)


def _compat_row_factory(cursor):
    names = tuple(column.name for column in (cursor.description or ()))
    return lambda values: _CompatRow(values, names)


def _postgres_cursor_factory():
    import psycopg

    class CompatCursor(psycopg.Cursor):
        def execute(self, query, params=None, *, prepare=None, binary=None):
            if isinstance(query, str):
                query = _postgres_sql(query)
            return super().execute(query, params, prepare=prepare, binary=binary)

        def executemany(self, query, params_seq, *, returning=False):
            if isinstance(query, str):
                query = _postgres_sql(query)
            return super().executemany(query, params_seq, returning=returning)

    return CompatCursor


SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    project_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    session_id TEXT,
    state TEXT NOT NULL,
    resume_state TEXT,
    current_plan_version_id TEXT,
    current_step_id TEXT,
    checkpoint_id TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    budget_json TEXT NOT NULL DEFAULT '{}',
    skill_names_json TEXT NOT NULL DEFAULT '[]',
    source_turn_id TEXT,
    source_plan_document_id TEXT,
    source_plan_document_version_id TEXT,
    source_plan_content_hash TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_source_turn
ON runs(source_turn_id)
WHERE source_turn_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    active_turn_id TEXT,
    next_event_seq INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    client_turn_id TEXT NOT NULL,
    parent_turn_id TEXT,
    status TEXT NOT NULL,
    policy TEXT,
    content_shape TEXT,
    reason_code TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    skill_names_json TEXT NOT NULL DEFAULT '[]',
    artifact_kind TEXT,
    artifact_operation TEXT,
    artifact_title TEXT,
    plan_context_document_id TEXT,
    plan_context_version_id TEXT,
    plan_context_version INTEGER,
    plan_context_hash TEXT,
    materialized_goal_id TEXT,
    materialized_run_id TEXT,
    direction_action TEXT,
    direction_idempotency_key TEXT UNIQUE,
    direction_projection_status TEXT,
    direction_projection_source_document_id TEXT,
    direction_projection_source_version_id TEXT,
    direction_projection_source_hash TEXT,
    direction_projection_draft_json TEXT,
    direction_projection_error TEXT,
    direction_projection_claim_owner TEXT,
    direction_projection_lease_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(thread_id, client_turn_id)
);
CREATE TABLE IF NOT EXISTS turn_asks (
    id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL UNIQUE,
    questions_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    answer_json TEXT,
    answer_idempotency_key TEXT UNIQUE,
    continuation_turn_id TEXT,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_turn_asks_turn_status ON turn_asks(turn_id, status);
CREATE TABLE IF NOT EXISTS turn_jobs (
    turn_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    lease_owner TEXT,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    cancel_requested_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    last_error_json TEXT
);
CREATE TABLE IF NOT EXISTS thread_messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1,
    content_length INTEGER NOT NULL DEFAULT 0,
    plan_document_version_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS thread_events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    seq INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    data_json TEXT NOT NULL,
    UNIQUE(thread_id, seq)
);
CREATE TRIGGER IF NOT EXISTS thread_events_append_only_update
BEFORE UPDATE ON thread_events
BEGIN
    SELECT RAISE(ABORT, 'thread events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS thread_events_append_only_delete
BEFORE DELETE ON thread_events
BEGIN
    SELECT RAISE(ABORT, 'thread events are append-only');
END;
CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_versions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    goal_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    summary TEXT NOT NULL DEFAULT '',
    base_version INTEGER,
    source_document_version_id TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    UNIQUE(run_id, version)
);
CREATE TABLE IF NOT EXISTS plan_steps (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL,
    plan_version_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    atomic INTEGER NOT NULL DEFAULT 1,
    completed_at TEXT,
    canceled_at TEXT,
    UNIQUE(plan_version_id, position),
    UNIQUE(plan_version_id, id)
);
CREATE TABLE IF NOT EXISTS plan_documents (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    current_version_id TEXT,
    projected_version_id TEXT,
    file_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS plan_document_versions (
    id TEXT PRIMARY KEY,
    plan_document_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    base_version_id TEXT,
    title TEXT NOT NULL,
    markdown_content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_turn_id TEXT,
    source_message_id TEXT,
    actor TEXT NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE(plan_document_id, version),
    UNIQUE(source_turn_id),
    UNIQUE(source_message_id)
);
CREATE TABLE IF NOT EXISTS plan_write_intents (
    id TEXT PRIMARY KEY,
    plan_document_id TEXT NOT NULL,
    version_id TEXT NOT NULL UNIQUE,
    expected_head_version_id TEXT,
    expected_file_hash TEXT,
    target_file_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_json TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    state TEXT NOT NULL,
    plan_version_id TEXT,
    step_id TEXT,
    payload_json TEXT NOT NULL,
    last_event_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    interaction_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    seq INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    correlation_json TEXT NOT NULL,
    data_json TEXT NOT NULL,
    UNIQUE(run_id, seq)
);
CREATE TRIGGER IF NOT EXISTS events_append_only_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS events_append_only_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    acted_at TEXT,
    expires_at TEXT,
    UNIQUE(run_id, tool_call_id, params_hash)
);
CREATE TABLE IF NOT EXISTS memory_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    scope TEXT NOT NULL,
    project_id TEXT,
    skill_name TEXT,
    confidence REAL NOT NULL,
    evidence_event_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    memory_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_file_versions (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(path, version)
);
CREATE TABLE IF NOT EXISTS run_stats (
    run_id TEXT PRIMARY KEY,
    projection_version INTEGER NOT NULL DEFAULT 1,
    stats_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""


MIGRATION_20260823 = r"""
CREATE TABLE IF NOT EXISTS memory_entries (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('preference','constraint','fact','decision','lesson')),
 scope_type TEXT NOT NULL CHECK(scope_type IN ('user','project')), scope_id TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','ARCHIVED','PURGED')), current_revision_id TEXT,
 canonical_fingerprint TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
 importance REAL NOT NULL DEFAULT .5 CHECK(importance BETWEEN 0 AND 1), sensitivity TEXT NOT NULL DEFAULT 'normal',
 valid_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_active_fingerprint ON memory_entries(owner_id,scope_type,scope_id,canonical_fingerprint) WHERE status='ACTIVE';
CREATE INDEX IF NOT EXISTS idx_memory_entries_scope ON memory_entries(owner_id,scope_type,scope_id,status,pinned,importance,updated_at);
CREATE TABLE IF NOT EXISTS memory_revisions (
 id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES memory_entries(id) ON DELETE CASCADE,
 revision_no INTEGER NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('CREATE','UPDATE','ROLLBACK','RESTORE')),
 content TEXT NOT NULL, content_hash TEXT NOT NULL, base_revision_id TEXT, actor TEXT NOT NULL,
 source_refs_json TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(entry_id,revision_no)
);
CREATE TABLE IF NOT EXISTS memory_proposals (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, operation TEXT NOT NULL CHECK(operation IN ('ADD','UPDATE','ARCHIVE')),
 target_entry_id TEXT, base_revision_id TEXT,
 kind TEXT NOT NULL CHECK(kind IN ('preference','constraint','fact','decision','lesson')),
 scope_type TEXT NOT NULL CHECK(scope_type IN ('user','project')), scope_id TEXT NOT NULL DEFAULT '',
 content TEXT NOT NULL, fingerprint TEXT NOT NULL, evidence_refs_json TEXT NOT NULL DEFAULT '[]', evidence_hash TEXT NOT NULL DEFAULT '',
 origin TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1), reason TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','ACCEPTED','REJECTED','SUPERSEDED')),
 request_idempotency_key TEXT NOT NULL UNIQUE, decision_idempotency_key TEXT UNIQUE, accepted_revision_id TEXT,
 created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS conversation_archive_state (
 owner_id TEXT NOT NULL, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
 archived_through_seq INTEGER NOT NULL DEFAULT 0, reserved_start_seq INTEGER, reserved_end_seq INTEGER, source_hash TEXT,
 state TEXT NOT NULL DEFAULT 'IDLE' CHECK(state IN ('IDLE','RESERVED','FAILED')), lease_owner TEXT, lease_until TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, PRIMARY KEY(owner_id,thread_id)
);
CREATE TABLE IF NOT EXISTS memory_episodes (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE, project_id TEXT,
 start_message_seq INTEGER NOT NULL, end_message_seq INTEGER NOT NULL, source_hash TEXT NOT NULL, summary TEXT NOT NULL,
 topics_json TEXT NOT NULL DEFAULT '[]', decisions_json TEXT NOT NULL DEFAULT '[]', open_loops_json TEXT NOT NULL DEFAULT '[]',
 sensitivity TEXT NOT NULL DEFAULT 'normal', retrieval_policy TEXT NOT NULL DEFAULT 'thread', retain_until TEXT,
 status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','ARCHIVED','DELETED','RAW_REFERENCE')),
 supersedes_episode_id TEXT, model_id TEXT, prompt_version TEXT NOT NULL DEFAULT 'episode-v1', created_at TEXT NOT NULL,
 UNIQUE(owner_id,thread_id,start_message_seq,end_message_seq,source_hash)
);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_scope ON memory_episodes(owner_id,thread_id,project_id,status,created_at);
CREATE TABLE IF NOT EXISTS memory_context_pins (
 model_invocation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, parent_type TEXT NOT NULL, parent_id TEXT NOT NULL, purpose TEXT NOT NULL,
 query_hash TEXT NOT NULL, scope_hash TEXT NOT NULL, revision_ids_json TEXT NOT NULL, episode_ids_json TEXT NOT NULL,
 tokenizer_version TEXT NOT NULL, renderer_version TEXT NOT NULL, budget_json TEXT NOT NULL, token_count INTEGER NOT NULL,
 rendered_hash TEXT NOT NULL, invocation_state TEXT NOT NULL DEFAULT 'PREPARED', invalidated_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_projection_intents (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, path TEXT NOT NULL, expected_hash TEXT, target_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('PENDING','COMMITTED','FAILED','CONFLICT')), attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS memory_audit_events (
 row_id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
 aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
 operation TEXT NOT NULL, actor TEXT NOT NULL, occurred_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', UNIQUE(owner_id,seq)
);
CREATE TABLE IF NOT EXISTS legacy_memory_mappings (legacy_id TEXT PRIMARY KEY, disposition TEXT NOT NULL, target_id TEXT, reason TEXT NOT NULL DEFAULT '');

CREATE TABLE IF NOT EXISTS research_jobs (
 id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id), source_turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id),
 schedule_id TEXT, retry_of_job_id TEXT REFERENCES research_jobs(id),
 trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('manual','scheduled','retry','run_now')), occurrence_key TEXT NOT NULL UNIQUE,
 topic TEXT NOT NULL CHECK(length(topic) BETWEEN 1 AND 2000), source_scopes_json TEXT NOT NULL DEFAULT '["web"]',
 status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED')),
 phase TEXT NOT NULL CHECK(phase IN ('queued','planning','retrieving','distilling','reflecting','curating','writing','summarizing','finalizing','completed','partial','failed','cancelled')),
 available_at TEXT NOT NULL, lease_owner TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2,
 cancel_requested_at TEXT, started_at TEXT, finished_at TEXT, last_error_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_claim ON research_jobs(status,available_at,lease_until,created_at);
CREATE INDEX IF NOT EXISTS idx_research_thread ON research_jobs(thread_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_schedule ON research_jobs(schedule_id,created_at DESC);
CREATE TABLE IF NOT EXISTS research_job_attempts (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE, attempt INTEGER NOT NULL,
 lease_owner TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED','LEASE_LOST','CANCELLED')),
 started_at TEXT NOT NULL, finished_at TEXT, error_json TEXT, UNIQUE(job_id,attempt)
);
CREATE TABLE IF NOT EXISTS research_reports (
 job_id TEXT PRIMARY KEY REFERENCES research_jobs(id) ON DELETE CASCADE, title TEXT, outline_json TEXT, summary_json TEXT, markdown TEXT,
 partial_markdown TEXT NOT NULL DEFAULT '', source_count INTEGER NOT NULL DEFAULT 0, evidence_count INTEGER NOT NULL DEFAULT 0,
 assistant_message_id TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS research_sources (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('web','local_note')), canonical_url TEXT, locator TEXT, title TEXT NOT NULL, content TEXT NOT NULL,
 content_hash TEXT NOT NULL, published_at TEXT, retrieved_at TEXT NOT NULL, quality_score REAL NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(job_id,ordinal), UNIQUE(job_id,kind,content_hash)
);
CREATE TABLE IF NOT EXISTS research_evidence (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES research_sources(id) ON DELETE CASCADE, text TEXT NOT NULL, date_hint TEXT,
 relevance REAL NOT NULL CHECK(relevance BETWEEN 0 AND 1), created_at TEXT NOT NULL, UNIQUE(job_id,source_id,text)
);
CREATE TABLE IF NOT EXISTS research_sections (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, heading TEXT NOT NULL,
 thesis TEXT NOT NULL DEFAULT '', evidence_ids_json TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL CHECK(status IN ('PENDING','WRITING','COMPLETED')), markdown TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
 generation INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, completed_at TEXT, UNIQUE(job_id,ordinal)
);
CREATE TABLE IF NOT EXISTS research_schedules (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, thread_id TEXT NOT NULL UNIQUE REFERENCES threads(id), topic TEXT NOT NULL,
 source_scopes_json TEXT NOT NULL DEFAULT '["web"]', trigger_type TEXT NOT NULL CHECK(trigger_type IN ('daily','weekly','interval_hours')),
 trigger_time TEXT, trigger_weekday INTEGER CHECK(trigger_weekday BETWEEN 0 AND 6), interval_hours INTEGER CHECK(interval_hours BETWEEN 1 AND 720),
 timezone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), notify_enabled INTEGER NOT NULL DEFAULT 1 CHECK(notify_enabled IN (0,1)),
 next_run_at TEXT, last_run_at TEXT, last_job_id TEXT, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((trigger_type='daily' AND trigger_time IS NOT NULL AND trigger_weekday IS NULL AND interval_hours IS NULL)
 OR (trigger_type='weekly' AND trigger_time IS NOT NULL AND trigger_weekday IS NOT NULL AND interval_hours IS NULL)
 OR (trigger_type='interval_hours' AND trigger_time IS NULL AND trigger_weekday IS NULL AND interval_hours IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS notification_channels (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, channel_type TEXT NOT NULL CHECK(channel_type IN ('serverchan','wecom','dingtalk','webhook')),
 secret_env_name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(channel_type,name)
);
CREATE TABLE IF NOT EXISTS notification_deliveries (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES research_jobs(id) ON DELETE CASCADE,
 channel_id TEXT NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE, attempt INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','SENT','FAILED')), http_status INTEGER, provider_code TEXT, error TEXT,
 created_at TEXT NOT NULL, finished_at TEXT, UNIQUE(job_id,channel_id,attempt)
);
CREATE TABLE IF NOT EXISTS app_settings (
 id INTEGER PRIMARY KEY CHECK(id=1), human_mode INTEGER NOT NULL DEFAULT 0 CHECK(human_mode IN (0,1)), updated_at TEXT NOT NULL
);
"""

MIGRATION_20260824_GOAL_PROGRAMS = r"""
CREATE TABLE IF NOT EXISTS goal_programs (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
 source_thread_id TEXT NOT NULL REFERENCES threads(id),
 source_plan_document_id TEXT NOT NULL REFERENCES plan_documents(id),
 source_plan_document_version_id TEXT NOT NULL REFERENCES plan_document_versions(id),
 source_plan_content_hash TEXT NOT NULL,
 objective_title TEXT NOT NULL DEFAULT '', objective_summary TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL CHECK(status IN ('DRAFT','ACTIVE','PAUSED','COMPLETED','CANCELLED')),
 compile_status TEXT NOT NULL CHECK(compile_status IN ('COMPILING','READY','FAILED')),
 compile_error_code TEXT, timezone TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
 daily_minutes INTEGER NOT NULL CHECK(daily_minutes BETWEEN 5 AND 1440),
 current_program_version_id TEXT, version INTEGER NOT NULL DEFAULT 0,
 next_event_seq INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT, cancelled_at TEXT, deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_goal_programs_owner_status ON goal_programs(owner_id,status,deleted_at,updated_at);
CREATE TABLE IF NOT EXISTS goal_program_versions (
 id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES goal_programs(id), version INTEGER NOT NULL,
 base_version_id TEXT REFERENCES goal_program_versions(id),
 source_plan_document_version_id TEXT NOT NULL REFERENCES plan_document_versions(id),
 structure_json TEXT NOT NULL, change_summary TEXT NOT NULL DEFAULT '', actor TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(program_id,version)
);
CREATE TABLE IF NOT EXISTS goal_actions (
 id TEXT PRIMARY KEY, program_id TEXT NOT NULL REFERENCES goal_programs(id),
 program_version_id TEXT NOT NULL REFERENCES goal_program_versions(id), logical_key TEXT NOT NULL,
 scheduled_date TEXT NOT NULL, position INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 estimated_minutes INTEGER NOT NULL CHECK(estimated_minutes BETWEEN 5 AND 180), completion_criteria TEXT NOT NULL,
 required INTEGER NOT NULL CHECK(required IN (0,1)),
 status TEXT NOT NULL CHECK(status IN ('SCHEDULED','COMPLETED','SKIPPED','DEFERRED','CANCELLED')),
 version INTEGER NOT NULL DEFAULT 0, completed_at TEXT, skipped_at TEXT, deferred_at TEXT, cancelled_at TEXT,
 deferred_from_action_id TEXT REFERENCES goal_actions(id), cancel_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(program_id,program_version_id,logical_key), UNIQUE(program_id,program_version_id,scheduled_date,position)
);
CREATE INDEX IF NOT EXISTS idx_goal_actions_today ON goal_actions(program_id,status,scheduled_date,position);
CREATE UNIQUE INDEX IF NOT EXISTS uq_goal_action_deferred_from ON goal_actions(deferred_from_action_id) WHERE deferred_from_action_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS goal_action_feedback (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, action_id TEXT NOT NULL REFERENCES goal_actions(id), kind TEXT NOT NULL,
 actual_minutes INTEGER, difficulty INTEGER, reason_code TEXT, note TEXT, sensitivity TEXT NOT NULL DEFAULT 'normal',
 idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(owner_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS goal_command_receipts (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
 operation TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(owner_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS goal_program_events (
 row_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 program_id TEXT NOT NULL REFERENCES goal_programs(id), seq INTEGER NOT NULL, action_id TEXT REFERENCES goal_actions(id),
 type TEXT NOT NULL, actor TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
 occurred_at TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', UNIQUE(program_id,seq)
);
CREATE TRIGGER IF NOT EXISTS goal_program_events_append_only_update BEFORE UPDATE ON goal_program_events
BEGIN SELECT RAISE(ABORT,'goal program events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS goal_program_events_append_only_delete BEFORE DELETE ON goal_program_events
BEGIN SELECT RAISE(ABORT,'goal program events are append-only'); END;
CREATE TABLE IF NOT EXISTS goal_adjustment_proposals (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, program_id TEXT NOT NULL REFERENCES goal_programs(id),
 base_program_version_id TEXT NOT NULL REFERENCES goal_program_versions(id),
 expected_plan_document_version_id TEXT NOT NULL REFERENCES plan_document_versions(id), expected_plan_content_hash TEXT NOT NULL,
 affected_actions_json TEXT NOT NULL, candidate_structure_json TEXT NOT NULL, diff_json TEXT NOT NULL, reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','ACCEPTED','REJECTED','STALE')), version INTEGER NOT NULL DEFAULT 0,
 accepted_program_version_id TEXT REFERENCES goal_program_versions(id),
 plan_sync_status TEXT, plan_sync_version_id TEXT REFERENCES plan_document_versions(id),
 created_at TEXT NOT NULL, decided_at TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goal_adjustments_program ON goal_adjustment_proposals(program_id,status,created_at);
"""

MIGRATION_20260824_GOAL_REVIEWS = r"""
CREATE TABLE IF NOT EXISTS goal_daily_reviews (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, program_id TEXT NOT NULL REFERENCES goal_programs(id), local_date TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','FAILED')), attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_until TEXT, source_hash TEXT NOT NULL, signals_json TEXT NOT NULL DEFAULT '[]',
 summary TEXT, encouragement TEXT, needs_adjustment INTEGER, adjustment_reason TEXT,
 proposal_id TEXT REFERENCES goal_adjustment_proposals(id), error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
 UNIQUE(owner_id,program_id,local_date)
);
CREATE INDEX IF NOT EXISTS idx_goal_daily_reviews_queue ON goal_daily_reviews(status,lease_until,created_at);
CREATE TABLE IF NOT EXISTS goal_review_action_snapshots (
 review_id TEXT NOT NULL REFERENCES goal_daily_reviews(id) ON DELETE CASCADE,
 action_id TEXT NOT NULL REFERENCES goal_actions(id), action_version INTEGER NOT NULL, status TEXT NOT NULL,
 title TEXT NOT NULL, estimated_minutes INTEGER NOT NULL, actual_minutes INTEGER, difficulty INTEGER,
 PRIMARY KEY(review_id,action_id)
);
"""

MIGRATION_20260824_GOAL_COMPLETION = r"""
ALTER TABLE goal_programs ADD COLUMN completion_summary TEXT;
ALTER TABLE goal_programs ADD COLUMN completion_episode_id TEXT;
"""

MIGRATION_20260824_AGENT_EVOLUTION = r"""
CREATE TABLE IF NOT EXISTS runtime_bundles (
 id TEXT PRIMARY KEY, bundle_hash TEXT NOT NULL UNIQUE, manifest_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_channels (
 name TEXT PRIMARY KEY, bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id), version INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_channel_events (
 id TEXT PRIMARY KEY, channel_name TEXT NOT NULL, from_bundle_id TEXT, to_bundle_id TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_context_snapshots (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 runtime_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id), created_at TEXT NOT NULL,
 UNIQUE(owner_id,content_hash,runtime_bundle_id)
);
CREATE TABLE IF NOT EXISTS agent_runs (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, thread_id TEXT, objective TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('single','expert')), status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','WAITING','SUCCEEDED','FAILED','CANCELLED')),
 context_snapshot_id TEXT NOT NULL REFERENCES agent_context_snapshots(id), runtime_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id),
 coordinator_task_id TEXT, next_event_seq INTEGER NOT NULL DEFAULT 1,
 budget_units INTEGER NOT NULL DEFAULT 0 CHECK(budget_units>=0), reserved_budget_units INTEGER NOT NULL DEFAULT 0 CHECK(reserved_budget_units>=0),
 idempotency_key TEXT NOT NULL UNIQUE, version INTEGER NOT NULL DEFAULT 0, cancel_requested_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_tasks (
 id TEXT PRIMARY KEY, agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
 root_task_id TEXT NOT NULL, parent_task_id TEXT REFERENCES agent_tasks(id), child_key TEXT,
 role TEXT NOT NULL, objective TEXT NOT NULL, context_snapshot_id TEXT NOT NULL REFERENCES agent_context_snapshots(id),
 output_schema TEXT NOT NULL DEFAULT 'artifact.v1', status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','WAITING_CHILDREN','SUCCEEDED','FAILED','CANCELLED')),
 priority INTEGER NOT NULL DEFAULT 0, join_policy TEXT CHECK(join_policy IN ('ALL_SUCCESS','ALL_DONE')), children_closed_at TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 2,
 lease_owner TEXT, lease_epoch INTEGER NOT NULL DEFAULT 0, lease_until TEXT, available_at TEXT NOT NULL,
 budget_units INTEGER NOT NULL DEFAULT 0 CHECK(budget_units>=0), result_artifact_id TEXT, error_code TEXT,
 cancel_requested_at TEXT, cancel_reason TEXT, version INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
 UNIQUE(parent_task_id,child_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_task_claim ON agent_tasks(status,available_at,lease_until,priority,created_at);
CREATE TABLE IF NOT EXISTS agent_task_attempts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
 attempt_no INTEGER NOT NULL, lease_owner TEXT NOT NULL, lease_epoch INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED','LEASE_LOST','CANCELLED')),
 started_at TEXT NOT NULL, heartbeat_at TEXT, finished_at TEXT, error_json TEXT, UNIQUE(task_id,attempt_no)
);
CREATE TABLE IF NOT EXISTS agent_task_checkpoints (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
 attempt_no INTEGER NOT NULL, lease_epoch INTEGER NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(task_id,attempt_no,payload_hash)
);
CREATE TABLE IF NOT EXISTS agent_artifacts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
 attempt_no INTEGER NOT NULL, lease_epoch INTEGER NOT NULL, artifact_type TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1,
 content_json TEXT NOT NULL, source_refs_json TEXT NOT NULL DEFAULT '[]', content_hash TEXT NOT NULL,
 redaction_status TEXT NOT NULL DEFAULT 'safe', created_at TEXT NOT NULL, UNIQUE(task_id,attempt_no,artifact_type)
);
CREATE TABLE IF NOT EXISTS agent_events (
 row_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 agent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
 task_id TEXT, type TEXT NOT NULL, actor TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}', occurred_at TEXT NOT NULL,
 UNIQUE(agent_run_id,seq)
);
CREATE TRIGGER IF NOT EXISTS agent_events_append_only_update BEFORE UPDATE ON agent_events
BEGIN SELECT RAISE(ABORT,'agent events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS agent_events_append_only_delete BEFORE DELETE ON agent_events
BEGIN SELECT RAISE(ABORT,'agent events are append-only'); END;
CREATE TABLE IF NOT EXISTS tool_execution_claims (
 logical_action_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT, agent_id TEXT,
 tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL, params_hash TEXT NOT NULL, target_hash TEXT,
 skill_digest TEXT, policy_version TEXT,
 status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED','RECONCILIATION_REQUIRED')),
 lease_epoch INTEGER NOT NULL DEFAULT 1, result_json TEXT, error_code TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
 UNIQUE(run_id,tool_call_id)
);
"""

MIGRATION_20260824_CONTROLLED_EVOLUTION = r"""
CREATE TABLE evolution_experiences (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, task_type TEXT NOT NULL, outcome TEXT NOT NULL,
 lineage_group_hash TEXT NOT NULL, source_content_hash TEXT NOT NULL,
 runtime_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id),
 dataset_partition TEXT NOT NULL CHECK(dataset_partition IN ('DISCOVERY','DEV','HOLDOUT','SAFETY')),
 request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
 UNIQUE(owner_id,lineage_group_hash)
);
CREATE TABLE evolution_candidates (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
 candidate_type TEXT NOT NULL CHECK(candidate_type IN ('memory','skill','policy','prompt','code')),
 experience_ids_json TEXT NOT NULL, base_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id),
 target_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id), target_bundle_digest TEXT NOT NULL,
 proposed_content_json TEXT NOT NULL, proposed_digest TEXT NOT NULL,
 permission_diff_json TEXT NOT NULL, permission_diff_digest TEXT NOT NULL, reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('READY_FOR_EVAL','EVALUATED','APPROVED','CANARY','PROMOTED','REJECTED','ROLLED_BACK')),
 version INTEGER NOT NULL DEFAULT 0, current_evaluation_id TEXT, approval_id TEXT, deployment_id TEXT,
 request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TRIGGER evolution_candidates_frozen BEFORE UPDATE ON evolution_candidates
WHEN (
 OLD.owner_id <> NEW.owner_id OR OLD.candidate_type <> NEW.candidate_type OR
 OLD.experience_ids_json <> NEW.experience_ids_json OR OLD.base_bundle_id <> NEW.base_bundle_id OR
 OLD.target_bundle_id <> NEW.target_bundle_id OR OLD.target_bundle_digest <> NEW.target_bundle_digest OR
 OLD.proposed_content_json <> NEW.proposed_content_json OR OLD.proposed_digest <> NEW.proposed_digest OR
 OLD.permission_diff_json <> NEW.permission_diff_json OR OLD.permission_diff_digest <> NEW.permission_diff_digest OR
 OLD.reason <> NEW.reason
)
BEGIN SELECT RAISE(ABORT,'evolution candidate is frozen'); END;
CREATE TABLE evolution_evaluations (
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES evolution_candidates(id),
 baseline_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id), candidate_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id),
 eval_set_digest TEXT NOT NULL, evaluator_digest TEXT NOT NULL,
 deterministic_pass INTEGER NOT NULL CHECK(deterministic_pass IN (0,1)), checks_json TEXT NOT NULL,
 metrics_json TEXT NOT NULL, report_digest TEXT NOT NULL UNIQUE, status TEXT NOT NULL CHECK(status IN ('COMPLETED')),
 request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, finished_at TEXT NOT NULL
);
CREATE TABLE evolution_decisions (
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES evolution_candidates(id),
 evaluation_id TEXT REFERENCES evolution_evaluations(id), decision TEXT NOT NULL CHECK(decision IN ('APPROVE','REJECT','PROMOTE','ROLLBACK')),
 from_bundle_id TEXT, to_bundle_id TEXT, actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 candidate_digest TEXT NOT NULL, evaluation_report_digest TEXT NOT NULL,
 permission_diff_digest TEXT NOT NULL, target_bundle_digest TEXT NOT NULL,
 expires_at TEXT, request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE canary_deployments (
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES evolution_candidates(id),
 approval_id TEXT NOT NULL REFERENCES evolution_decisions(id),
 champion_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id), challenger_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id),
 allocation_percent INTEGER NOT NULL CHECK(allocation_percent BETWEEN 1 AND 100), assignment_unit TEXT NOT NULL,
 salt_digest TEXT NOT NULL, gate_policy_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','PROMOTED','ROLLED_BACK','STOPPED')),
 request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE canary_exposures (
 deployment_id TEXT NOT NULL REFERENCES canary_deployments(id), run_id TEXT NOT NULL,
 assignment_hash TEXT NOT NULL, cohort TEXT NOT NULL CHECK(cohort IN ('champion','challenger')),
 bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id), success INTEGER NOT NULL CHECK(success IN (0,1)),
 safety_pass INTEGER NOT NULL CHECK(safety_pass IN (0,1)), request_digest TEXT NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE, exposed_at TEXT NOT NULL, PRIMARY KEY(deployment_id,run_id)
);
CREATE TABLE evolution_events (
 row_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 candidate_id TEXT, type TEXT NOT NULL, actor TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}',
 idempotency_key TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL
);
CREATE TRIGGER evolution_events_append_only_update BEFORE UPDATE ON evolution_events
BEGIN SELECT RAISE(ABORT,'evolution events are append-only'); END;
CREATE TRIGGER evolution_events_append_only_delete BEFORE DELETE ON evolution_events
BEGIN SELECT RAISE(ABORT,'evolution events are append-only'); END;
CREATE INDEX idx_evolution_candidates_owner_status ON evolution_candidates(owner_id,status,created_at);
CREATE INDEX idx_evolution_events_candidate ON evolution_events(candidate_id,row_id);
"""

MIGRATIONS = (
    (1, MIGRATION_20260823),
    (2, MIGRATION_20260824_GOAL_PROGRAMS),
    (3, MIGRATION_20260824_GOAL_REVIEWS),
    (4, MIGRATION_20260824_GOAL_COMPLETION),
    (5, MIGRATION_20260824_AGENT_EVOLUTION),
    (6, MIGRATION_20260824_CONTROLLED_EVOLUTION),
    (7, r"""
    ALTER TABLE evolution_experiences ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'manual';
    ALTER TABLE evolution_experiences ADD COLUMN source_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE evolution_experiences ADD COLUMN source_event_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE evolution_experiences ADD COLUMN signal_type TEXT NOT NULL DEFAULT 'manual';
    ALTER TABLE evolution_experiences ADD COLUMN severity TEXT NOT NULL DEFAULT 'info';
    ALTER TABLE evolution_experiences ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE evolution_experiences ADD COLUMN failure_tags_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE evolution_experiences ADD COLUMN observed_at TEXT NOT NULL DEFAULT '';
    CREATE UNIQUE INDEX uq_experience_observation ON evolution_experiences(owner_id,source_kind,source_event_id,signal_type) WHERE source_event_id <> '';
    CREATE INDEX idx_experience_observer_source ON evolution_experiences(owner_id,source_kind,source_id,created_at);
    CREATE TABLE evolution_observer_offsets (
      stream_kind TEXT NOT NULL, owner_id TEXT NOT NULL, last_row_id INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL, PRIMARY KEY(stream_kind,owner_id)
    );
    """),
    (8, r"""
    ALTER TABLE canary_exposures RENAME TO canary_exposures_v7;
    CREATE TABLE canary_exposures (
      deployment_id TEXT NOT NULL REFERENCES canary_deployments(id), run_id TEXT NOT NULL,
      assignment_hash TEXT NOT NULL, cohort TEXT NOT NULL CHECK(cohort IN ('champion','challenger')),
      bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id), success INTEGER CHECK(success IN (0,1)),
      safety_pass INTEGER CHECK(safety_pass IN (0,1)), request_digest TEXT NOT NULL,
      idempotency_key TEXT NOT NULL UNIQUE, exposed_at TEXT NOT NULL, finished_at TEXT,
      PRIMARY KEY(deployment_id,run_id)
    );
    INSERT INTO canary_exposures(deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,request_digest,idempotency_key,exposed_at,finished_at)
    SELECT deployment_id,run_id,assignment_hash,cohort,bundle_id,success,safety_pass,request_digest,idempotency_key,exposed_at,exposed_at FROM canary_exposures_v7;
    DROP TABLE canary_exposures_v7;
    """),
    (9, r"""
    CREATE TABLE model_profiles (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('ACTIVE','DISABLED')),
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(owner_id,name)
    );
    CREATE TABLE model_profile_versions (
      id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES model_profiles(id), version INTEGER NOT NULL,
      provider_protocol TEXT NOT NULL CHECK(provider_protocol IN ('openai_compatible','anthropic','gemini')),
      provider_name TEXT NOT NULL, base_url TEXT NOT NULL, model_name TEXT NOT NULL, credential_env_ref TEXT NOT NULL,
      capabilities_json TEXT NOT NULL, context_window INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL,
      timeout_seconds REAL NOT NULL, max_attempts INTEGER NOT NULL, config_digest TEXT NOT NULL,
      created_at TEXT NOT NULL, UNIQUE(profile_id,version), UNIQUE(profile_id,config_digest)
    );
    CREATE TRIGGER model_profile_versions_frozen BEFORE UPDATE ON model_profile_versions
    BEGIN SELECT RAISE(ABORT,'model profile version is frozen'); END;
    CREATE TRIGGER model_profile_versions_no_delete BEFORE DELETE ON model_profile_versions
    BEGIN SELECT RAISE(ABORT,'model profile version is frozen'); END;
    CREATE TABLE model_routing_policies (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
      roles_json TEXT NOT NULL, policy_digest TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
      UNIQUE(owner_id,name,version)
    );
    CREATE TRIGGER model_routing_policies_frozen BEFORE UPDATE ON model_routing_policies
    BEGIN SELECT RAISE(ABORT,'model routing policy is frozen'); END;
    CREATE TRIGGER model_routing_policies_no_delete BEFORE DELETE ON model_routing_policies
    BEGIN SELECT RAISE(ABORT,'model routing policy is frozen'); END;
    CREATE TABLE model_invocations (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, run_id TEXT, thread_id TEXT, turn_id TEXT, agent_task_id TEXT,
      role TEXT NOT NULL, purpose TEXT NOT NULL, runtime_bundle_id TEXT, routing_policy_id TEXT,
      routing_policy_digest TEXT NOT NULL, route_snapshot_json TEXT NOT NULL, request_digest TEXT NOT NULL,
      tool_schema_digest TEXT NOT NULL, context_snapshot_digest TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('CREATED','ROUTED','RUNNING','SUCCEEDED','FAILED','CANCELLED','BUDGET_BLOCKED')),
      selected_attempt_id TEXT, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, finished_at TEXT
    );
    CREATE INDEX idx_model_invocations_run ON model_invocations(run_id,created_at);
    CREATE INDEX idx_model_invocations_thread ON model_invocations(thread_id,created_at);
    CREATE TABLE model_attempts (
      id TEXT PRIMARY KEY, invocation_id TEXT NOT NULL REFERENCES model_invocations(id), ordinal INTEGER NOT NULL,
      reason TEXT NOT NULL CHECK(reason IN ('primary','retry','fallback','repair')),
      profile_version_id TEXT NOT NULL REFERENCES model_profile_versions(id), provider_protocol TEXT NOT NULL,
      provider_request_id TEXT, request_digest TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('STARTED','SUCCEEDED','FAILED','CANCELLED')),
      error_kind TEXT, http_status INTEGER,
      uncached_input_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
      output_tokens INTEGER, reasoning_tokens INTEGER,
      started_at TEXT NOT NULL, first_token_at TEXT, finished_at TEXT,
      usage_status TEXT NOT NULL DEFAULT 'UNAVAILABLE', usage_digest TEXT,
      UNIQUE(invocation_id,ordinal)
    );
    CREATE INDEX idx_model_attempts_invocation ON model_attempts(invocation_id,ordinal);
    """),
    (10, r"""
    CREATE TABLE model_price_snapshots (
      id TEXT PRIMARY KEY, profile_version_id TEXT NOT NULL REFERENCES model_profile_versions(id),
      uncached_input_rate INTEGER NOT NULL, cache_read_rate INTEGER NOT NULL, cache_write_rate INTEGER NOT NULL,
      output_rate INTEGER NOT NULL, reasoning_rate INTEGER NOT NULL,
      currency TEXT NOT NULL DEFAULT 'USD', effective_at TEXT NOT NULL, price_digest TEXT NOT NULL UNIQUE,
      created_at TEXT NOT NULL
    );
    CREATE TRIGGER model_price_snapshots_frozen BEFORE UPDATE ON model_price_snapshots
    BEGIN SELECT RAISE(ABORT,'model price snapshot is frozen'); END;
    CREATE TRIGGER model_price_snapshots_no_delete BEFORE DELETE ON model_price_snapshots
    BEGIN SELECT RAISE(ABORT,'model price snapshot is frozen'); END;
    CREATE TABLE cost_budgets (
      owner_id TEXT NOT NULL, period_kind TEXT NOT NULL CHECK(period_kind IN ('INVOCATION','DAILY','MONTHLY')),
      period_key TEXT NOT NULL, limit_microusd INTEGER NOT NULL CHECK(limit_microusd >= 0),
      reserved_microusd INTEGER NOT NULL DEFAULT 0 CHECK(reserved_microusd >= 0),
      charged_microusd INTEGER NOT NULL DEFAULT 0 CHECK(charged_microusd >= 0),
      version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
      PRIMARY KEY(owner_id,period_kind,period_key)
    );
    CREATE TABLE cost_ledger (
      row_id INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL,
      period_kind TEXT NOT NULL, period_key TEXT NOT NULL, invocation_id TEXT NOT NULL,
      attempt_id TEXT NOT NULL, price_snapshot_id TEXT,
      entry_type TEXT NOT NULL CHECK(entry_type IN ('RESERVE','CHARGE','RELEASE','ADJUSTMENT')),
      amount_microusd INTEGER NOT NULL CHECK(amount_microusd >= 0), cost_status TEXT NOT NULL,
      reason TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
    );
    CREATE INDEX idx_cost_ledger_owner_period ON cost_ledger(owner_id,period_kind,period_key,row_id);
    CREATE UNIQUE INDEX uq_cost_attempt_entry ON cost_ledger(attempt_id,entry_type,COALESCE(price_snapshot_id,''));
    CREATE TRIGGER cost_ledger_append_only_update BEFORE UPDATE ON cost_ledger
    BEGIN SELECT RAISE(ABORT,'cost ledger is append-only'); END;
    CREATE TRIGGER cost_ledger_append_only_delete BEFORE DELETE ON cost_ledger
    BEGIN SELECT RAISE(ABORT,'cost ledger is append-only'); END;
    ALTER TABLE model_attempts ADD COLUMN price_snapshot_id TEXT REFERENCES model_price_snapshots(id);
    ALTER TABLE model_attempts ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'UNAVAILABLE';
    ALTER TABLE model_attempts ADD COLUMN cost_microusd INTEGER;
    CREATE TABLE evaluation_runs (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, suite_id TEXT NOT NULL, suite_digest TEXT NOT NULL,
      baseline_bundle_id TEXT NOT NULL, candidate_bundle_id TEXT NOT NULL, evaluator_digest TEXT NOT NULL,
      status TEXT NOT NULL, budget_microusd INTEGER NOT NULL, created_at TEXT NOT NULL, finished_at TEXT
    );
    CREATE TABLE evaluation_case_pairs (
      id TEXT PRIMARY KEY, evaluation_run_id TEXT NOT NULL REFERENCES evaluation_runs(id), case_id TEXT NOT NULL,
      partition TEXT NOT NULL CHECK(partition IN ('DEV','HOLDOUT','SAFETY')), domain TEXT NOT NULL,
      input_digest TEXT NOT NULL, fixture_digest TEXT NOT NULL, execution_order TEXT NOT NULL,
      UNIQUE(evaluation_run_id,case_id)
    );
    CREATE TABLE evaluation_arm_results (
      id TEXT PRIMARY KEY, case_pair_id TEXT NOT NULL REFERENCES evaluation_case_pairs(id), arm TEXT NOT NULL CHECK(arm IN ('A','B')),
      bundle_id TEXT NOT NULL, invocation_id TEXT REFERENCES model_invocations(id), output_text TEXT NOT NULL,
      output_digest TEXT NOT NULL, deterministic_pass INTEGER NOT NULL, metrics_json TEXT NOT NULL,
      UNIQUE(case_pair_id,arm)
    );
    CREATE TABLE evaluation_judgments (
      id TEXT PRIMARY KEY, case_pair_id TEXT NOT NULL REFERENCES evaluation_case_pairs(id), kind TEXT NOT NULL CHECK(kind IN ('QUALITY','SAFETY')),
      judge_invocation_id TEXT REFERENCES model_invocations(id), result_json TEXT NOT NULL, judgment_digest TEXT NOT NULL UNIQUE,
      created_at TEXT NOT NULL, UNIQUE(case_pair_id,kind)
    );
    """),
    (11, r"""
    CREATE TABLE skills (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('INSTALLED','ENABLED','DISABLED','UNINSTALLED')),
      default_version_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, uninstalled_at TEXT,
      UNIQUE(owner_id,name)
    );
    CREATE TABLE skill_versions (
      id TEXT PRIMARY KEY, skill_id TEXT NOT NULL REFERENCES skills(id), version TEXT NOT NULL,
      package_digest TEXT NOT NULL, manifest_digest TEXT NOT NULL, manifest_json TEXT NOT NULL,
      title TEXT NOT NULL, description TEXT NOT NULL, content TEXT NOT NULL, requested_tools_json TEXT NOT NULL,
      connectors_json TEXT NOT NULL, phases_json TEXT NOT NULL, storage_path TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('INSTALLED','ENABLED','DISABLED','UNINSTALLED')),
      created_at TEXT NOT NULL, UNIQUE(skill_id,version), UNIQUE(package_digest)
    );
    CREATE TRIGGER skill_versions_frozen BEFORE UPDATE ON skill_versions
    WHEN OLD.skill_id<>NEW.skill_id OR OLD.version<>NEW.version OR OLD.package_digest<>NEW.package_digest OR
         OLD.manifest_digest<>NEW.manifest_digest OR OLD.manifest_json<>NEW.manifest_json OR
         OLD.title<>NEW.title OR OLD.description<>NEW.description OR OLD.content<>NEW.content OR
         OLD.requested_tools_json<>NEW.requested_tools_json OR OLD.connectors_json<>NEW.connectors_json OR
         OLD.phases_json<>NEW.phases_json OR OLD.storage_path<>NEW.storage_path OR OLD.created_at<>NEW.created_at
    BEGIN SELECT RAISE(ABORT,'skill version is frozen'); END;
    CREATE TRIGGER skill_versions_no_delete BEFORE DELETE ON skill_versions
    BEGIN SELECT RAISE(ABORT,'skill version is frozen'); END;
    CREATE TABLE skill_grants (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, skill_version_id TEXT NOT NULL REFERENCES skill_versions(id),
      granted_tools_json TEXT NOT NULL, grant_digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ACTIVE','REVOKED')),
      version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(owner_id,skill_version_id)
    );
    CREATE TABLE skill_bindings (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, binding_type TEXT NOT NULL CHECK(binding_type IN ('THREAD','RUN')),
      binding_id TEXT NOT NULL, version_ids_json TEXT NOT NULL, snapshot_digest TEXT NOT NULL,
      idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(owner_id,binding_type,binding_id)
    );
    CREATE TABLE trusted_connectors (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ENABLED','DISABLED')),
      current_version_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner_id,name)
    );
    CREATE TABLE trusted_connector_versions (
      id TEXT PRIMARY KEY, connector_id TEXT NOT NULL REFERENCES trusted_connectors(id), version INTEGER NOT NULL,
      base_url TEXT NOT NULL, methods_json TEXT NOT NULL, paths_json TEXT NOT NULL, credential_env_ref TEXT,
      request_schema_json TEXT NOT NULL DEFAULT '{}', timeout_seconds REAL NOT NULL, max_response_bytes INTEGER NOT NULL,
      risk TEXT NOT NULL, config_digest TEXT NOT NULL UNIQUE, verified_at TEXT,
      created_at TEXT NOT NULL, UNIQUE(connector_id,version)
    );
    CREATE TRIGGER trusted_connector_versions_frozen BEFORE UPDATE ON trusted_connector_versions
    BEGIN SELECT RAISE(ABORT,'trusted connector version is frozen'); END;
    CREATE TRIGGER trusted_connector_versions_no_delete BEFORE DELETE ON trusted_connector_versions
    BEGIN SELECT RAISE(ABORT,'trusted connector version is frozen'); END;
    CREATE TABLE skill_events (
      row_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL,
      skill_id TEXT, skill_version_id TEXT, type TEXT NOT NULL, actor TEXT NOT NULL,
      data_json TEXT NOT NULL DEFAULT '{}', idempotency_key TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL
    );
    CREATE INDEX idx_skill_events_skill ON skill_events(skill_id,row_id);
    CREATE TRIGGER skill_events_append_only_update BEFORE UPDATE ON skill_events
    BEGIN SELECT RAISE(ABORT,'skill events are append-only'); END;
    CREATE TRIGGER skill_events_append_only_delete BEFORE DELETE ON skill_events
    BEGIN SELECT RAISE(ABORT,'skill events are append-only'); END;
    ALTER TABLE approvals ADD COLUMN binding_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE approvals ADD COLUMN binding_digest TEXT NOT NULL DEFAULT '';
    """),
    (12, r"""
    DROP TRIGGER model_profile_versions_frozen;
    DROP TRIGGER trusted_connector_versions_frozen;
    ALTER TABLE model_profile_versions ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE';
    ALTER TABLE model_profile_versions ADD COLUMN verified_at TEXT;
    ALTER TABLE model_profile_versions ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'UNVERIFIED';
    ALTER TABLE model_profile_versions ADD COLUMN verification_error_kind TEXT;
    ALTER TABLE evaluation_runs ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE evaluation_runs ADD COLUMN lease_owner TEXT;
    ALTER TABLE evaluation_runs ADD COLUMN lease_until TEXT;
    ALTER TABLE evaluation_runs ADD COLUMN cancel_requested_at TEXT;
    ALTER TABLE evaluation_runs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE evaluation_runs ADD COLUMN updated_at TEXT;
    ALTER TABLE evaluation_runs ADD COLUMN idempotency_key TEXT;
    ALTER TABLE evaluation_runs ADD COLUMN request_digest TEXT;
    ALTER TABLE evaluation_runs ADD COLUMN error_json TEXT;
    CREATE UNIQUE INDEX uq_evaluation_run_idempotency ON evaluation_runs(owner_id,idempotency_key) WHERE idempotency_key IS NOT NULL;
    CREATE INDEX idx_evaluation_run_claim ON evaluation_runs(status,lease_until,created_at);
    CREATE TRIGGER model_profile_versions_frozen BEFORE UPDATE ON model_profile_versions
    WHEN OLD.profile_id<>NEW.profile_id OR OLD.version<>NEW.version OR OLD.provider_protocol<>NEW.provider_protocol OR
         OLD.provider_name<>NEW.provider_name OR OLD.base_url<>NEW.base_url OR OLD.model_name<>NEW.model_name OR
         OLD.credential_env_ref<>NEW.credential_env_ref OR OLD.capabilities_json<>NEW.capabilities_json OR
         OLD.context_window<>NEW.context_window OR OLD.max_output_tokens<>NEW.max_output_tokens OR
         OLD.timeout_seconds<>NEW.timeout_seconds OR OLD.max_attempts<>NEW.max_attempts OR
         OLD.config_digest<>NEW.config_digest OR OLD.created_at<>NEW.created_at
    BEGIN SELECT RAISE(ABORT,'model profile version is frozen'); END;
    CREATE TRIGGER trusted_connector_versions_frozen BEFORE UPDATE ON trusted_connector_versions
    WHEN OLD.connector_id<>NEW.connector_id OR OLD.version<>NEW.version OR OLD.base_url<>NEW.base_url OR
         OLD.methods_json<>NEW.methods_json OR OLD.paths_json<>NEW.paths_json OR
         COALESCE(OLD.credential_env_ref,'')<>COALESCE(NEW.credential_env_ref,'') OR
         OLD.request_schema_json<>NEW.request_schema_json OR OLD.timeout_seconds<>NEW.timeout_seconds OR
         OLD.max_response_bytes<>NEW.max_response_bytes OR OLD.risk<>NEW.risk OR
         OLD.config_digest<>NEW.config_digest OR OLD.created_at<>NEW.created_at
    BEGIN SELECT RAISE(ABORT,'trusted connector version is frozen'); END;
    """),
    (13, r"""
    ALTER TABLE runs ADD COLUMN runtime_bundle_id TEXT REFERENCES runtime_bundles(id);
    ALTER TABLE turns ADD COLUMN runtime_bundle_id TEXT REFERENCES runtime_bundles(id);
    CREATE INDEX idx_runs_runtime_bundle ON runs(runtime_bundle_id);
    CREATE INDEX idx_turns_runtime_bundle ON turns(runtime_bundle_id);
    """),
    (14, r"""
    DROP INDEX uq_cost_attempt_entry;
    CREATE UNIQUE INDEX uq_cost_attempt_period_entry ON cost_ledger(
      attempt_id,period_kind,period_key,entry_type,COALESCE(price_snapshot_id,'')
    );
    """),
    (15, r"""
    ALTER TABLE canary_exposures ADD COLUMN quality_outcome TEXT;
    ALTER TABLE canary_exposures ADD COLUMN safety_outcome TEXT;
    ALTER TABLE canary_exposures ADD COLUMN ttft_ms INTEGER;
    ALTER TABLE canary_exposures ADD COLUMN ttft_p95_ms INTEGER;
    ALTER TABLE canary_exposures ADD COLUMN invocation_count INTEGER;
    ALTER TABLE canary_exposures ADD COLUMN attempt_count INTEGER;
    ALTER TABLE canary_exposures ADD COLUMN cost_microusd INTEGER;
    ALTER TABLE canary_exposures ADD COLUMN routing_policy_digest TEXT;
    ALTER TABLE canary_exposures ADD COLUMN profile_digest TEXT;
    ALTER TABLE canary_exposures ADD COLUMN skill_digest TEXT;
    """),
    (16, r"""
    ALTER TABLE skill_bindings ADD COLUMN grant_snapshots_json TEXT NOT NULL DEFAULT '{}';
    """),
    (17, r"""
    CREATE TABLE IF NOT EXISTS evaluation_events (
      row_id INTEGER PRIMARY KEY AUTOINCREMENT,
      evaluation_run_id TEXT NOT NULL REFERENCES evaluation_runs(id),
      seq INTEGER NOT NULL,
      type TEXT NOT NULL,
      data_json TEXT NOT NULL DEFAULT '{}',
      idempotency_key TEXT NOT NULL UNIQUE,
      occurred_at TEXT NOT NULL,
      UNIQUE(evaluation_run_id,seq)
    );
    CREATE TRIGGER IF NOT EXISTS evaluation_events_append_only_update BEFORE UPDATE ON evaluation_events
    BEGIN SELECT RAISE(ABORT,'evaluation events are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS evaluation_events_append_only_delete BEFORE DELETE ON evaluation_events
    BEGIN SELECT RAISE(ABORT,'evaluation events are append-only'); END;
    """),
    (18, r"""
    ALTER TABLE conversation_archive_state ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE memory_episodes ADD COLUMN schema_version TEXT NOT NULL DEFAULT 'episode-v1';
    ALTER TABLE memory_episodes ADD COLUMN synopsis_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE memory_episodes ADD COLUMN outcomes_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE memory_episodes ADD COLUMN source_message_ids_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE memory_episodes ADD COLUMN source_token_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE memory_episodes ADD COLUMN summary_token_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE memory_episodes ADD COLUMN tokenizer_version TEXT NOT NULL DEFAULT 'utf8-upper-bound-v1';
    ALTER TABLE memory_episodes ADD COLUMN deleted_at TEXT;
    CREATE TABLE memory_archive_jobs (
      id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
      start_message_seq INTEGER NOT NULL,
      end_message_seq INTEGER NOT NULL,
      source_hash TEXT NOT NULL,
      prompt_version TEXT NOT NULL,
      tokenizer_version TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','RETRY_WAIT','COMPLETED','DEAD_LETTER','LEASE_LOST')),
      available_at TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      max_attempts INTEGER NOT NULL DEFAULT 3,
      lease_owner TEXT,
      lease_epoch INTEGER NOT NULL DEFAULT 0,
      lease_until TEXT,
      last_error_code TEXT,
      last_error_json TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      finished_at TEXT,
      UNIQUE(owner_id,thread_id,start_message_seq,end_message_seq,source_hash,prompt_version)
    );
    CREATE INDEX idx_memory_archive_jobs_claim ON memory_archive_jobs(status,available_at,lease_until,created_at);
    CREATE TABLE memory_context_pin_items (
      pin_invocation_id TEXT NOT NULL REFERENCES memory_context_pins(model_invocation_id) ON DELETE CASCADE,
      source_type TEXT NOT NULL CHECK(source_type IN ('revision','episode','thread')),
      source_id TEXT NOT NULL,
      source_version TEXT NOT NULL DEFAULT '',
      PRIMARY KEY(pin_invocation_id,source_type,source_id)
    );
    CREATE INDEX idx_memory_context_pin_source ON memory_context_pin_items(source_type,source_id);
    CREATE TABLE memory_context_pin_payloads (
      pin_invocation_id TEXT PRIMARY KEY REFERENCES memory_context_pins(model_invocation_id) ON DELETE CASCADE,
      rendered TEXT NOT NULL,
      payload_hash TEXT NOT NULL,
      expires_at TEXT NOT NULL
    );
    ALTER TABLE memory_context_pins ADD COLUMN binding_hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE memory_context_pins ADD COLUMN expires_at TEXT;
    ALTER TABLE memory_context_pins ADD COLUMN invalidation_reason TEXT;
    CREATE UNIQUE INDEX uq_threads_id_owner ON threads(id,owner_id);
    CREATE TRIGGER memory_episode_scope_insert BEFORE INSERT ON memory_episodes
    WHEN NOT EXISTS (
      SELECT 1 FROM threads t WHERE t.id=NEW.thread_id AND t.owner_id=NEW.owner_id
      AND COALESCE(t.project_id,'')=COALESCE(NEW.project_id,'')
    )
    BEGIN SELECT RAISE(ABORT,'memory episode scope mismatch'); END;
    CREATE TRIGGER memory_episode_scope_update BEFORE UPDATE OF owner_id,thread_id,project_id ON memory_episodes
    WHEN NOT EXISTS (
      SELECT 1 FROM threads t WHERE t.id=NEW.thread_id AND t.owner_id=NEW.owner_id
      AND COALESCE(t.project_id,'')=COALESCE(NEW.project_id,'')
    )
    BEGIN SELECT RAISE(ABORT,'memory episode scope mismatch'); END;
    CREATE TRIGGER archive_state_scope_insert BEFORE INSERT ON conversation_archive_state
    WHEN NOT EXISTS (SELECT 1 FROM threads t WHERE t.id=NEW.thread_id AND t.owner_id=NEW.owner_id)
    BEGIN SELECT RAISE(ABORT,'archive state scope mismatch'); END;
    """),
    (19, r"""
    CREATE TABLE memory_archive_signals (
      turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
      thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
      created_at TEXT NOT NULL
    );
    CREATE INDEX idx_memory_archive_signals_created ON memory_archive_signals(created_at,turn_id);
    CREATE TRIGGER memory_archive_signal_on_completed
    AFTER UPDATE OF status ON turns
    WHEN NEW.status='COMPLETED' AND OLD.status<>'COMPLETED'
    BEGIN
      INSERT OR IGNORE INTO memory_archive_signals(turn_id,thread_id,created_at)
      VALUES (NEW.id,NEW.thread_id,datetime('now'));
    END;
    """),
    (20, r"""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_proposals_owner_request
      ON memory_proposals(owner_id,request_idempotency_key);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_proposals_owner_decision
      ON memory_proposals(owner_id,decision_idempotency_key)
      WHERE decision_idempotency_key IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_audit_owner_key
      ON memory_audit_events(owner_id,idempotency_key);
    """),
    (21, r"""
    ALTER TABLE memory_entries ADD COLUMN evidence_state TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'
      CHECK(evidence_state IN ('VERIFIED','LEGACY_UNVERIFIED','INVALID'));
    ALTER TABLE memory_proposals ADD COLUMN request_digest TEXT NOT NULL DEFAULT '';
    ALTER TABLE memory_proposals ADD COLUMN decision_request_digest TEXT;
    ALTER TABLE memory_proposals ADD COLUMN evidence_state TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'
      CHECK(evidence_state IN ('VERIFIED','LEGACY_UNVERIFIED','INVALID'));
    ALTER TABLE memory_proposals ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE memory_proposals ADD COLUMN accepted_content TEXT;
    ALTER TABLE memory_audit_events ADD COLUMN request_digest TEXT NOT NULL DEFAULT '';
    CREATE TABLE memory_evidence_links (
      id TEXT PRIMARY KEY,
      aggregate_type TEXT NOT NULL CHECK(aggregate_type IN ('proposal','revision')),
      aggregate_id TEXT NOT NULL,
      source_type TEXT NOT NULL CHECK(source_type IN ('thread_message','thread_event','run_event')),
      source_id TEXT NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(aggregate_type,aggregate_id,source_type,source_id)
    );
    CREATE INDEX idx_memory_evidence_aggregate
      ON memory_evidence_links(aggregate_type,aggregate_id,created_at,id);
    UPDATE memory_proposals
      SET status='SUPERSEDED', evidence_state='LEGACY_UNVERIFIED', version=version+1
      WHERE status='PENDING';
    CREATE UNIQUE INDEX uq_memory_pending_add_fingerprint
      ON memory_proposals(owner_id,scope_type,scope_id,fingerprint)
      WHERE operation='ADD' AND status='PENDING';
    """),
    (22, r"""
    ALTER TABLE memory_episodes ADD COLUMN version INTEGER NOT NULL DEFAULT 0;
    """),
    (23, r"""
    CREATE TABLE turn_metrics (
      turn_id TEXT PRIMARY KEY REFERENCES turns(id),
      queue_wait_ms INTEGER,
      context_ms INTEGER,
      model_ttft_ms INTEGER,
      stream_ms INTEGER,
      answer_wait_ms INTEGER,
      total_ms INTEGER,
      model_attempt_count INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_turn_jobs_claim ON turn_jobs(status,lease_until,started_at);
    CREATE INDEX idx_turns_thread_status ON turns(thread_id,status,id);
    """),
    (24, r"""
    ALTER TABLE turn_jobs ADD COLUMN thread_id TEXT;
    ALTER TABLE turn_jobs ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0;
    UPDATE turn_jobs SET thread_id=(SELECT thread_id FROM turns WHERE turns.id=turn_jobs.turn_id)
      WHERE thread_id IS NULL;
    CREATE UNIQUE INDEX uq_turn_jobs_one_running_per_thread
      ON turn_jobs(thread_id) WHERE status='RUNNING';
    """),
    (25, r"""
    ALTER TABLE agent_runs ADD COLUMN root_budget_id TEXT;
    ALTER TABLE research_jobs ADD COLUMN root_budget_id TEXT;
    ALTER TABLE evaluation_runs ADD COLUMN root_budget_id TEXT;
    ALTER TABLE turns ADD COLUMN root_budget_id TEXT;
    """),
    (26, r"""
    ALTER TABLE runs ADD COLUMN root_budget_id TEXT;
    ALTER TABLE memory_archive_jobs ADD COLUMN runtime_bundle_id TEXT;
    ALTER TABLE memory_archive_jobs ADD COLUMN root_budget_id TEXT;
    """),
    (27, r"""
    ALTER TABLE evolution_experiences ADD COLUMN root_task_id TEXT NOT NULL DEFAULT '';
    ALTER TABLE evolution_experiences ADD COLUMN target_role TEXT NOT NULL DEFAULT '';
    ALTER TABLE evolution_experiences ADD COLUMN provenance TEXT NOT NULL DEFAULT 'production';
    ALTER TABLE evolution_experiences ADD COLUMN source_version TEXT NOT NULL DEFAULT '';
    ALTER TABLE evolution_experiences ADD COLUMN source_state TEXT NOT NULL DEFAULT 'ACTIVE';
    ALTER TABLE evolution_experiences ADD COLUMN runtime_bundle_known INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE evolution_candidates ADD COLUMN contract_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE evolution_candidates ADD COLUMN release_contract_version TEXT NOT NULL DEFAULT '';
    ALTER TABLE evolution_observer_offsets ADD COLUMN last_success_row_id INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE evolution_observer_offsets ADD COLUMN last_error TEXT;
    ALTER TABLE evolution_observer_offsets ADD COLUMN last_error_at TEXT;
    ALTER TABLE canary_deployments ADD COLUMN target_role TEXT NOT NULL DEFAULT '';
    ALTER TABLE canary_deployments ADD COLUMN target_purpose TEXT NOT NULL DEFAULT '';
    ALTER TABLE canary_deployments ADD COLUMN budget_microusd INTEGER;
    ALTER TABLE canary_deployments ADD COLUMN deadline_at TEXT;
    ALTER TABLE canary_deployments ADD COLUMN release_contract_version TEXT NOT NULL DEFAULT '';
    ALTER TABLE canary_exposures ADD COLUMN target_role TEXT NOT NULL DEFAULT '';
    ALTER TABLE canary_exposures ADD COLUMN target_purpose TEXT NOT NULL DEFAULT '';
    ALTER TABLE canary_exposures ADD COLUMN prompt_hit INTEGER;
    CREATE INDEX idx_evolution_experience_root ON evolution_experiences(owner_id,root_task_id,created_at);
    CREATE INDEX idx_evolution_experience_source_state ON evolution_experiences(owner_id,source_state,created_at);
    CREATE TABLE evolution_content_authorizations (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, subject_id TEXT NOT NULL,
      source_scope_json TEXT NOT NULL, purpose TEXT NOT NULL,
      expires_at TEXT NOT NULL, revoked_at TEXT, created_at TEXT NOT NULL,
      request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE
    );
    CREATE TABLE evolution_generation_batches (
      id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, problem_fingerprint TEXT NOT NULL,
      experience_ids_json TEXT NOT NULL, base_bundle_id TEXT NOT NULL REFERENCES runtime_bundles(id),
      target_role TEXT NOT NULL, allowed_path TEXT NOT NULL, generation_config_digest TEXT NOT NULL,
      root_budget_id TEXT NOT NULL, max_calls INTEGER NOT NULL CHECK(max_calls > 0),
      budget_microusd INTEGER NOT NULL CHECK(budget_microusd > 0), deadline_at TEXT NOT NULL,
      content_authorization_id TEXT REFERENCES evolution_content_authorizations(id),
      status TEXT NOT NULL CHECK(status IN ('APPROVED','REQUESTING','COMPLETED','STOPPED','UNKNOWN')),
      calls_started INTEGER NOT NULL DEFAULT 0, candidate_id TEXT REFERENCES evolution_candidates(id),
      error_json TEXT, request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(owner_id,problem_fingerprint,base_bundle_id,generation_config_digest)
    );
    CREATE INDEX idx_evolution_generation_status ON evolution_generation_batches(owner_id,status,created_at);
    CREATE TRIGGER evolution_candidate_contract_frozen BEFORE UPDATE OF contract_json,release_contract_version ON evolution_candidates
    BEGIN SELECT RAISE(ABORT,'evolution candidate contract is frozen'); END;
    """),
    (28, r"""
    ALTER TABLE model_invocations ADD COLUMN system_prompt_digest TEXT NOT NULL DEFAULT '';
    ALTER TABLE canary_deployments ADD COLUMN stable_version INTEGER;
    ALTER TABLE canary_deployments ADD COLUMN stop_reason TEXT;
    ALTER TABLE canary_deployments ADD COLUMN max_calls INTEGER;
    ALTER TABLE canary_exposures ADD COLUMN prompt_digest TEXT;
    CREATE TABLE evolution_release_suites (digest TEXT PRIMARY KEY, owner_id TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE canary_attempt_reservations (attempt_id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES canary_deployments(id), amount_microusd INTEGER NOT NULL CHECK(amount_microusd >= 0), created_at TEXT NOT NULL);
    CREATE TRIGGER evolution_release_suites_frozen BEFORE UPDATE ON evolution_release_suites BEGIN SELECT RAISE(ABORT,'release suite is frozen'); END;
    CREATE TRIGGER canary_attempt_reservations_frozen BEFORE UPDATE ON canary_attempt_reservations BEGIN SELECT RAISE(ABORT,'canary reservation is frozen'); END;
    """),
)


from .learning_schema import STATEMENTS as LEARNING_SCHEMA_STATEMENTS

MIGRATIONS = (*MIGRATIONS, (29, ";\n".join(LEARNING_SCHEMA_STATEMENTS) + ";"))
from .learning_assets import STATEMENTS as LEARNING_ASSET_STATEMENTS
MIGRATIONS = (*MIGRATIONS, (30, ";\n".join(LEARNING_ASSET_STATEMENTS) + ";"))
MIGRATIONS = (*MIGRATIONS, (31, """
    CREATE TABLE evolution_release_suites_owner (
      digest TEXT NOT NULL, owner_id TEXT NOT NULL, metadata_json TEXT NOT NULL,
      created_at TEXT NOT NULL, PRIMARY KEY(owner_id,digest)
    );
    INSERT INTO evolution_release_suites_owner SELECT * FROM evolution_release_suites;
    DROP TABLE evolution_release_suites;
    ALTER TABLE evolution_release_suites_owner RENAME TO evolution_release_suites;
    CREATE TRIGGER evolution_release_suites_frozen BEFORE UPDATE ON evolution_release_suites
      BEGIN SELECT RAISE(ABORT,'release suite is frozen'); END;
"""))
MIGRATIONS = (*MIGRATIONS, (32, "-- Rebuild skill_versions without globally unique package_digest; preserve IDs and immutable triggers."))
MIGRATIONS = (*MIGRATIONS, (33, """
    ALTER TABLE model_price_snapshots ADD COLUMN source_url TEXT NOT NULL DEFAULT 'legacy:unspecified';
"""))
MIGRATIONS = (*MIGRATIONS, (34, """
    ALTER TABLE goal_programs ADD COLUMN schedule_constraints_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE goal_actions ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE goal_action_feedback ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE goal_daily_reviews ADD COLUMN evidence_stale INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE goal_daily_reviews ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE goal_daily_reviews ADD COLUMN history_json TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE goal_review_action_snapshots ADD COLUMN feedback_json TEXT NOT NULL DEFAULT '{}';
"""))


MIGRATIONS = (*MIGRATIONS, (35, """
    ALTER TABLE goal_daily_reviews ADD COLUMN adjustment_status TEXT NOT NULL DEFAULT 'NOT_NEEDED';
    ALTER TABLE goal_daily_reviews ADD COLUMN adjustment_error_code TEXT;
    ALTER TABLE goal_daily_reviews ADD COLUMN adjustment_attempts INTEGER NOT NULL DEFAULT 0;
    UPDATE goal_daily_reviews SET adjustment_status='COMPLETED' WHERE proposal_id IS NOT NULL;
"""))


# R1 budget contract. ``validation_tier`` stays NULL for legacy versions, which
# keep the historical C - O - margin formula. A tier-A version must carry an
# evidenced ``admitted_context_limit`` and ``counter_evidence_version``; the
# repository default window is not capacity evidence.
MIGRATIONS = (*MIGRATIONS, (36, """
    DROP TRIGGER model_profile_versions_frozen;
    ALTER TABLE model_profile_versions ADD COLUMN admitted_context_limit INTEGER;
    ALTER TABLE model_profile_versions ADD COLUMN soft_context_limit INTEGER;
    ALTER TABLE model_profile_versions ADD COLUMN context_window_verified INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE model_profile_versions ADD COLUMN validation_tier TEXT;
    ALTER TABLE model_profile_versions ADD COLUMN counter_id TEXT NOT NULL DEFAULT 'utf8-upper-bound';
    ALTER TABLE model_profile_versions ADD COLUMN counter_version TEXT NOT NULL DEFAULT 'utf8-upper-bound-v1';
    ALTER TABLE model_profile_versions ADD COLUMN counter_evidence_version TEXT;
    ALTER TABLE model_profile_versions ADD COLUMN capacity_evidence TEXT;
    ALTER TABLE model_profile_versions ADD COLUMN protocol_budget_json TEXT NOT NULL DEFAULT '{}';
    CREATE TRIGGER model_profile_versions_frozen BEFORE UPDATE ON model_profile_versions
    WHEN OLD.profile_id<>NEW.profile_id OR OLD.version<>NEW.version OR OLD.provider_protocol<>NEW.provider_protocol OR
         OLD.provider_name<>NEW.provider_name OR OLD.base_url<>NEW.base_url OR OLD.model_name<>NEW.model_name OR
         OLD.credential_env_ref<>NEW.credential_env_ref OR OLD.capabilities_json<>NEW.capabilities_json OR
         OLD.context_window<>NEW.context_window OR OLD.max_output_tokens<>NEW.max_output_tokens OR
         OLD.timeout_seconds<>NEW.timeout_seconds OR OLD.max_attempts<>NEW.max_attempts OR
         OLD.config_digest<>NEW.config_digest OR OLD.created_at<>NEW.created_at OR
         COALESCE(OLD.admitted_context_limit,-1)<>COALESCE(NEW.admitted_context_limit,-1) OR
         COALESCE(OLD.soft_context_limit,-1)<>COALESCE(NEW.soft_context_limit,-1) OR
         OLD.context_window_verified<>NEW.context_window_verified OR
         COALESCE(OLD.validation_tier,'')<>COALESCE(NEW.validation_tier,'') OR
         OLD.counter_id<>NEW.counter_id OR OLD.counter_version<>NEW.counter_version OR
         COALESCE(OLD.counter_evidence_version,'')<>COALESCE(NEW.counter_evidence_version,'') OR
         COALESCE(OLD.capacity_evidence,'')<>COALESCE(NEW.capacity_evidence,'') OR
         OLD.protocol_budget_json<>NEW.protocol_budget_json
    BEGIN SELECT RAISE(ABORT,'model profile version is frozen'); END;
"""))

MIGRATIONS = (*MIGRATIONS, (37, """
    -- Invocation-scoped events. ``record_event`` used to drop every event
    -- unless the call happened to carry a thread/turn or run/goal id, so a
    -- capacity rejection or a skipped fallback on a context without those ids
    -- left no trace. These records must survive on the invocation alone.
    CREATE TABLE model_invocation_events (
      id TEXT PRIMARY KEY,
      invocation_id TEXT NOT NULL REFERENCES model_invocations(id),
      event_type TEXT NOT NULL,
      data_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX idx_model_invocation_events_invocation
      ON model_invocation_events(invocation_id, created_at);
"""))

MIGRATIONS = (*MIGRATIONS, (38, """
    -- The R1 selection policy belongs on the immutable profile version. Without
    -- these columns ``hot_window`` could only ever see its defaults, so the
    -- "policy comes from the versioned profile" claim was not actually true.
    DROP TRIGGER model_profile_versions_frozen;
    ALTER TABLE model_profile_versions ADD COLUMN history_min_turns INTEGER;
    ALTER TABLE model_profile_versions ADD COLUMN compact_ratio REAL;
    CREATE TRIGGER model_profile_versions_frozen BEFORE UPDATE ON model_profile_versions
    WHEN OLD.profile_id<>NEW.profile_id OR OLD.version<>NEW.version OR OLD.provider_protocol<>NEW.provider_protocol OR
         OLD.provider_name<>NEW.provider_name OR OLD.base_url<>NEW.base_url OR OLD.model_name<>NEW.model_name OR
         OLD.credential_env_ref<>NEW.credential_env_ref OR OLD.capabilities_json<>NEW.capabilities_json OR
         OLD.context_window<>NEW.context_window OR OLD.max_output_tokens<>NEW.max_output_tokens OR
         OLD.timeout_seconds<>NEW.timeout_seconds OR OLD.max_attempts<>NEW.max_attempts OR
         OLD.config_digest<>NEW.config_digest OR OLD.created_at<>NEW.created_at OR
         COALESCE(OLD.admitted_context_limit,-1)<>COALESCE(NEW.admitted_context_limit,-1) OR
         COALESCE(OLD.soft_context_limit,-1)<>COALESCE(NEW.soft_context_limit,-1) OR
         OLD.context_window_verified<>NEW.context_window_verified OR
         COALESCE(OLD.validation_tier,'')<>COALESCE(NEW.validation_tier,'') OR
         OLD.counter_id<>NEW.counter_id OR OLD.counter_version<>NEW.counter_version OR
         COALESCE(OLD.counter_evidence_version,'')<>COALESCE(NEW.counter_evidence_version,'') OR
         COALESCE(OLD.capacity_evidence,'')<>COALESCE(NEW.capacity_evidence,'') OR
         OLD.protocol_budget_json<>NEW.protocol_budget_json OR
         COALESCE(OLD.history_min_turns,-1)<>COALESCE(NEW.history_min_turns,-1) OR
         COALESCE(OLD.compact_ratio,-1.0)<>COALESCE(NEW.compact_ratio,-1.0)
    BEGIN SELECT RAISE(ABORT,'model profile version is frozen'); END;
"""))

MIGRATIONS = (*MIGRATIONS, (39, """
    -- R2-01: a tool result read must be scope-checked. ``call_id`` is already a
    -- stable identifier, but the owning scope could only be reached by joining
    -- through turns -> threads. Persisting it directly makes the check cheap and
    -- keeps a paged read from having to trust the caller's thread argument.
    ALTER TABLE turn_asks ADD COLUMN owner_id TEXT;
    UPDATE turn_asks SET owner_id = (
      SELECT t.owner_id FROM turns tt JOIN threads t ON t.id = tt.thread_id WHERE tt.id = turn_asks.turn_id
    ) WHERE owner_id IS NULL;
    CREATE INDEX idx_turn_asks_owner ON turn_asks(owner_id, call_id);
"""))

MIGRATIONS = (*MIGRATIONS, (40, """
    -- R2-02: the recent-window ceiling ``R`` is versioned profile policy, for
    -- the same reason ``history_min_turns`` and ``compact_ratio`` are. A policy
    -- value that cannot survive a round trip through the database is a default
    -- with extra steps, and ``hot_window`` would silently ignore a profile that
    -- declared a non-default ceiling.
    DROP TRIGGER model_profile_versions_frozen;
    ALTER TABLE model_profile_versions ADD COLUMN recent_window_bytes INTEGER;
    ALTER TABLE model_profile_versions ADD COLUMN recent_window_ratio REAL;
    CREATE TRIGGER model_profile_versions_frozen BEFORE UPDATE ON model_profile_versions
    WHEN OLD.profile_id<>NEW.profile_id OR OLD.version<>NEW.version OR OLD.provider_protocol<>NEW.provider_protocol OR
         OLD.provider_name<>NEW.provider_name OR OLD.base_url<>NEW.base_url OR OLD.model_name<>NEW.model_name OR
         OLD.credential_env_ref<>NEW.credential_env_ref OR OLD.capabilities_json<>NEW.capabilities_json OR
         OLD.context_window<>NEW.context_window OR OLD.max_output_tokens<>NEW.max_output_tokens OR
         OLD.timeout_seconds<>NEW.timeout_seconds OR OLD.max_attempts<>NEW.max_attempts OR
         OLD.config_digest<>NEW.config_digest OR OLD.created_at<>NEW.created_at OR
         COALESCE(OLD.admitted_context_limit,-1)<>COALESCE(NEW.admitted_context_limit,-1) OR
         COALESCE(OLD.soft_context_limit,-1)<>COALESCE(NEW.soft_context_limit,-1) OR
         OLD.context_window_verified<>NEW.context_window_verified OR
         COALESCE(OLD.validation_tier,'')<>COALESCE(NEW.validation_tier,'') OR
         OLD.counter_id<>NEW.counter_id OR OLD.counter_version<>NEW.counter_version OR
         COALESCE(OLD.counter_evidence_version,'')<>COALESCE(NEW.counter_evidence_version,'') OR
         COALESCE(OLD.capacity_evidence,'')<>COALESCE(NEW.capacity_evidence,'') OR
         OLD.protocol_budget_json<>NEW.protocol_budget_json OR
         COALESCE(OLD.history_min_turns,-1)<>COALESCE(NEW.history_min_turns,-1) OR
         COALESCE(OLD.compact_ratio,-1.0)<>COALESCE(NEW.compact_ratio,-1.0) OR
         COALESCE(OLD.recent_window_bytes,-1)<>COALESCE(NEW.recent_window_bytes,-1) OR
         COALESCE(OLD.recent_window_ratio,-1.0)<>COALESCE(NEW.recent_window_ratio,-1.0)
    BEGIN SELECT RAISE(ABORT,'model profile version is frozen'); END;
"""))

MIGRATIONS = (*MIGRATIONS, (41, """
    -- R2-03: the static early-archival line is versioned profile policy for the
    -- same reason R2-02's ceiling is. Without these columns a profile that
    -- declared a non-default trigger would be silently ignored and the selector
    -- would fall back to the 30%/20% defaults.
    DROP TRIGGER model_profile_versions_frozen;
    ALTER TABLE model_profile_versions ADD COLUMN archive_trigger_ratio REAL;
    ALTER TABLE model_profile_versions ADD COLUMN archive_reserve_ratio REAL;
    ALTER TABLE model_profile_versions ADD COLUMN archive_prefix_reserve INTEGER;
    CREATE TRIGGER model_profile_versions_frozen BEFORE UPDATE ON model_profile_versions
    WHEN OLD.profile_id<>NEW.profile_id OR OLD.version<>NEW.version OR OLD.provider_protocol<>NEW.provider_protocol OR
         OLD.provider_name<>NEW.provider_name OR OLD.base_url<>NEW.base_url OR OLD.model_name<>NEW.model_name OR
         OLD.credential_env_ref<>NEW.credential_env_ref OR OLD.capabilities_json<>NEW.capabilities_json OR
         OLD.context_window<>NEW.context_window OR OLD.max_output_tokens<>NEW.max_output_tokens OR
         OLD.timeout_seconds<>NEW.timeout_seconds OR OLD.max_attempts<>NEW.max_attempts OR
         OLD.config_digest<>NEW.config_digest OR OLD.created_at<>NEW.created_at OR
         COALESCE(OLD.admitted_context_limit,-1)<>COALESCE(NEW.admitted_context_limit,-1) OR
         COALESCE(OLD.soft_context_limit,-1)<>COALESCE(NEW.soft_context_limit,-1) OR
         OLD.context_window_verified<>NEW.context_window_verified OR
         COALESCE(OLD.validation_tier,'')<>COALESCE(NEW.validation_tier,'') OR
         OLD.counter_id<>NEW.counter_id OR OLD.counter_version<>NEW.counter_version OR
         COALESCE(OLD.counter_evidence_version,'')<>COALESCE(NEW.counter_evidence_version,'') OR
         COALESCE(OLD.capacity_evidence,'')<>COALESCE(NEW.capacity_evidence,'') OR
         OLD.protocol_budget_json<>NEW.protocol_budget_json OR
         COALESCE(OLD.history_min_turns,-1)<>COALESCE(NEW.history_min_turns,-1) OR
         COALESCE(OLD.compact_ratio,-1.0)<>COALESCE(NEW.compact_ratio,-1.0) OR
         COALESCE(OLD.recent_window_bytes,-1)<>COALESCE(NEW.recent_window_bytes,-1) OR
         COALESCE(OLD.recent_window_ratio,-1.0)<>COALESCE(NEW.recent_window_ratio,-1.0) OR
         COALESCE(OLD.archive_trigger_ratio,-1.0)<>COALESCE(NEW.archive_trigger_ratio,-1.0) OR
         COALESCE(OLD.archive_reserve_ratio,-1.0)<>COALESCE(NEW.archive_reserve_ratio,-1.0) OR
         COALESCE(OLD.archive_prefix_reserve,-1)<>COALESCE(NEW.archive_prefix_reserve,-1)
    BEGIN SELECT RAISE(ABORT,'model profile version is frozen'); END;
    -- "明确每个 Job 的来源范围和预算版本". The source scope is already pinned by
    -- start/end sequence plus source_hash; the budget version was not recorded,
    -- so a job could not be audited against the policy that created it.
    ALTER TABLE memory_archive_jobs ADD COLUMN budget_policy_version TEXT;
    ALTER TABLE memory_archive_jobs ADD COLUMN budget_profile_version_id TEXT;
"""))


MIGRATIONS = (*MIGRATIONS, (42, """
    -- D2: durable pending business-tool calls issued from a plain chat turn.
    -- A READ call is executed inline and only needs its result replayed into
    -- later transcripts. A WRITE call pauses the turn: the row keeps the tool
    -- identity and parameters while the existing approvals table carries the
    -- approval binding, so approval, replay and idempotency stay on the same
    -- server-side machinery the Agent Runtime already uses.
    CREATE TABLE IF NOT EXISTS turn_tool_calls (
        id TEXT PRIMARY KEY,
        turn_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        params_json TEXT NOT NULL,
        params_hash TEXT NOT NULL,
        risk TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
        approval_id TEXT,
        binding_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT,
        error_code TEXT,
        continuation_turn_id TEXT,
        decision_idempotency_key TEXT UNIQUE,
        created_at TEXT NOT NULL,
        acted_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_turn_tool_calls_turn ON turn_tool_calls(turn_id, status);
    CREATE INDEX IF NOT EXISTS idx_turn_tool_calls_continuation ON turn_tool_calls(continuation_turn_id);
"""))


class Database:
    def __init__(self, path: str | Path, workspace: str | Path | None = None) -> None:
        self.backend = "postgresql" if _is_postgres_url(path) else "sqlite"
        self.database_url = str(path) if self.backend == "postgresql" else None
        self._pool = None
        if self.backend == "postgresql":
            if workspace is None:
                raise ValueError("workspace is required for PostgreSQL databases")
            self.workspace = Path(workspace)
            self.workspace.mkdir(parents=True, exist_ok=True)
            self.path = self.workspace.parent / "agent.db"
            self._initialize_postgres_pool()
            self._validate_postgres_schema()
            return
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(workspace) if workspace else self.path.parent / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize_postgres_pool(self) -> None:
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            self.database_url,
            min_size=1,
            max_size=16,
            kwargs={
                "autocommit": True,
                "cursor_factory": _postgres_cursor_factory(),
                "row_factory": _compat_row_factory,
            },
            open=True,
        )

    def _validate_postgres_schema(self) -> None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            ).fetchone()
            extensions = {
                item["extname"]
                for item in connection.execute(
                    "SELECT extname FROM pg_extension WHERE extname IN ('vector','pg_trgm')"
                ).fetchall()
            }
        version = row["version_num"] if row is not None else None
        if not _postgres_schema_is_current(version, extensions):
            raise RuntimeError("PostgreSQL schema is not migrated; run alembic upgrade head")

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            ":memory:" if str(self.path) == ":memory:" else self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            self._add_column(connection, "threads", "owner_id TEXT NOT NULL DEFAULT 'local-user'")
            self._add_column(connection, "threads", "project_id TEXT")
            self._add_column(connection, "threads", "deleted_at TEXT")
            self._add_column(connection, "thread_messages", "message_seq INTEGER")
            self._add_column(connection, "thread_messages", "presentation TEXT NOT NULL DEFAULT 'standard'")
            self._add_column(connection, "thread_messages", "research_job_id TEXT")
            connection.execute(
                "WITH ranked AS (SELECT id,thread_id,ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY created_at,id) offset "
                "FROM thread_messages WHERE message_seq IS NULL), bases AS (SELECT thread_id,COALESCE(MAX(message_seq),0) base "
                "FROM thread_messages WHERE message_seq IS NOT NULL GROUP BY thread_id) UPDATE thread_messages SET message_seq="
                "COALESCE((SELECT base FROM bases WHERE bases.thread_id=thread_messages.thread_id),0)+"
                "(SELECT offset FROM ranked WHERE ranked.id=thread_messages.id) WHERE message_seq IS NULL"
            )
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_message_seq ON thread_messages(thread_id,message_seq)")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_message_research ON thread_messages(research_job_id) WHERE research_job_id IS NOT NULL")
            for version, sql in MIGRATIONS:
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                row = connection.execute("SELECT checksum FROM schema_migrations WHERE version=?", (version,)).fetchone()
                if row and row["checksum"] != checksum:
                    raise RuntimeError(f"schema migration {version} checksum mismatch")
                if not row:
                    if version == 13:
                        self._recover_migration_13(connection)
                    elif version == 14:
                        self._recover_migration_14(connection)
                    elif version == 15:
                        self._recover_migration_15(connection)
                    elif version == 16:
                        self._recover_migration_16(connection)
                    elif version == 17:
                        self._recover_migration_17(connection)
                    elif version == 18:
                        self._recover_migration_18(connection)
                    elif version == 19:
                        self._recover_migration_19(connection)
                    elif version == 20:
                        self._recover_migration_20(connection)
                    elif version == 21:
                        self._recover_migration_21(connection)
                    elif version == 22:
                        self._recover_migration_22(connection)
                    elif version == 23:
                        self._recover_migration_23(connection)
                    elif version == 26:
                        self._recover_migration_26(connection)
                    elif version == 32:
                        self._recover_migration_32(connection)
                    else:
                        connection.executescript(sql)
                    connection.execute(
                        "INSERT INTO schema_migrations(version,checksum,applied_at) VALUES (?,?,datetime('now'))",
                        (version, checksum),
                    )
            connection.execute("INSERT OR IGNORE INTO app_settings(id,human_mode,updated_at) VALUES (1,0,datetime('now'))")
            try:
                connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(entry_id UNINDEXED,owner_id UNINDEXED,content,tokenize='trigram')")
            except sqlite3.OperationalError:
                connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(entry_id UNINDEXED,owner_id UNINDEXED,content,tokenize='unicode61')")
            plan_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(plan_versions)").fetchall()
            }
            if "goal_id" not in plan_columns:
                connection.execute(
                    "ALTER TABLE plan_versions ADD COLUMN goal_id TEXT NOT NULL DEFAULT ''"
                )
            approval_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(approvals)").fetchall()
            }
            if "params_json" not in approval_columns:
                connection.execute(
                    "ALTER TABLE approvals ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}'"
                )
            run_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "skill_names_json" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN skill_names_json TEXT NOT NULL DEFAULT '[]'")
            if "source_turn_id" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN source_turn_id TEXT")
            if "source_plan_document_id" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN source_plan_document_id TEXT")
            if "source_plan_document_version_id" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN source_plan_document_version_id TEXT")
            if "source_plan_content_hash" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN source_plan_content_hash TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_source_turn "
                "ON runs(source_turn_id) WHERE source_turn_id IS NOT NULL"
            )
            turn_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(turns)").fetchall()
            }
            if "skill_names_json" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN skill_names_json TEXT NOT NULL DEFAULT '[]'")
            if "artifact_kind" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN artifact_kind TEXT")
            if "artifact_operation" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN artifact_operation TEXT")
            if "artifact_title" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN artifact_title TEXT")
            if "plan_context_document_id" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN plan_context_document_id TEXT")
            if "plan_context_version_id" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN plan_context_version_id TEXT")
            if "plan_context_version" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN plan_context_version INTEGER")
            if "plan_context_hash" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN plan_context_hash TEXT")
            if "direction_projection_status" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN direction_projection_status TEXT")
            if "direction_projection_source_document_id" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN direction_projection_source_document_id TEXT")
            if "direction_projection_source_version_id" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN direction_projection_source_version_id TEXT")
            if "direction_projection_source_hash" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN direction_projection_source_hash TEXT")
            if "direction_projection_draft_json" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN direction_projection_draft_json TEXT")
            if "direction_projection_error" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN direction_projection_error TEXT")
            if "direction_projection_claim_owner" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN direction_projection_claim_owner TEXT")
            if "direction_projection_lease_until" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN direction_projection_lease_until TEXT")
            if "goal_action_id" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN goal_action_id TEXT")
            message_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(thread_messages)").fetchall()
            }
            plan_document_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(plan_documents)").fetchall()
            }
            if "deleted_at" not in plan_document_columns:
                connection.execute("ALTER TABLE plan_documents ADD COLUMN deleted_at TEXT")
            if "plan_document_version_id" not in message_columns:
                connection.execute("ALTER TABLE thread_messages ADD COLUMN plan_document_version_id TEXT")
            if "source_document_version_id" not in plan_columns:
                connection.execute("ALTER TABLE plan_versions ADD COLUMN source_document_version_id TEXT")
            step_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(plan_steps)").fetchall()
            }
            if "row_id" not in step_columns:
                connection.execute("ALTER TABLE plan_steps RENAME TO plan_steps_legacy")
                connection.executescript(
                    """
                    CREATE TABLE plan_steps (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL,
                        plan_version_id TEXT NOT NULL,
                        position INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'pending',
                        atomic INTEGER NOT NULL DEFAULT 1,
                        completed_at TEXT,
                        canceled_at TEXT,
                        UNIQUE(plan_version_id, position),
                        UNIQUE(plan_version_id, id)
                    );
                    INSERT INTO plan_steps(
                        id, plan_version_id, position, title, description, status,
                        atomic, completed_at, canceled_at
                    )
                    SELECT id, plan_version_id, position, title, description, status,
                           atomic, completed_at, canceled_at
                    FROM plan_steps_legacy;
                    DROP TABLE plan_steps_legacy;
                    """
                )
        finally:
            connection.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.backend == "postgresql":
            with self._pool.connection() as connection:
                yield connection
            return
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.backend == "postgresql":
            with self._pool.connection() as connection:
                with connection.transaction():
                    yield connection
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _recover_migration_32(connection: sqlite3.Connection) -> None:
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='skill_versions'").fetchone()[0]
        if "UNIQUE(package_digest)" not in schema:
            return
        triggers = [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='skill_versions'")]
        schema = schema.replace("CREATE TABLE skill_versions", "CREATE TABLE skill_versions_owner", 1).replace(", UNIQUE(package_digest)", "")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(schema)
            connection.execute("INSERT INTO skill_versions_owner SELECT * FROM skill_versions")
            connection.execute("DROP TABLE skill_versions")
            connection.execute("ALTER TABLE skill_versions_owner RENAME TO skill_versions")
            for trigger in triggers:
                connection.execute(trigger)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("skill ownership migration violated a reference")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _recover_migration_13(connection: sqlite3.Connection) -> None:
        run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
        turn_columns = {row["name"] for row in connection.execute("PRAGMA table_info(turns)")}
        if "runtime_bundle_id" not in run_columns:
            connection.execute("ALTER TABLE runs ADD COLUMN runtime_bundle_id TEXT REFERENCES runtime_bundles(id)")
        if "runtime_bundle_id" not in turn_columns:
            connection.execute("ALTER TABLE turns ADD COLUMN runtime_bundle_id TEXT REFERENCES runtime_bundles(id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_runs_runtime_bundle ON runs(runtime_bundle_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_turns_runtime_bundle ON turns(runtime_bundle_id)")

    @staticmethod
    def _recover_migration_14(connection: sqlite3.Connection) -> None:
        connection.execute("DROP INDEX IF EXISTS uq_cost_attempt_entry")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_attempt_period_entry ON cost_ledger("
            "attempt_id,period_kind,period_key,entry_type,COALESCE(price_snapshot_id,''))"
        )

    @staticmethod
    def _recover_migration_15(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(canary_exposures)")}
        for definition in (
            "quality_outcome TEXT", "safety_outcome TEXT", "ttft_ms INTEGER", "ttft_p95_ms INTEGER",
            "invocation_count INTEGER", "attempt_count INTEGER", "cost_microusd INTEGER",
            "routing_policy_digest TEXT", "profile_digest TEXT", "skill_digest TEXT",
        ):
            if definition.split()[0] not in columns:
                connection.execute(f"ALTER TABLE canary_exposures ADD COLUMN {definition}")

    @staticmethod
    def _recover_migration_16(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(skill_bindings)")}
        if "grant_snapshots_json" not in columns:
            connection.execute("ALTER TABLE skill_bindings ADD COLUMN grant_snapshots_json TEXT NOT NULL DEFAULT '{}'")

    @staticmethod
    def _recover_migration_23(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS turn_metrics ("
            "turn_id TEXT PRIMARY KEY REFERENCES turns(id),queue_wait_ms INTEGER,context_ms INTEGER,"
            "model_ttft_ms INTEGER,stream_ms INTEGER,answer_wait_ms INTEGER,total_ms INTEGER,"
            "model_attempt_count INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_turn_jobs_claim ON turn_jobs(status,lease_until,started_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_turns_thread_status ON turns(thread_id,status,id)"
        )

    @staticmethod
    def _recover_migration_26(connection: sqlite3.Connection) -> None:
        Database._add_column(connection, "runs", "root_budget_id TEXT")
        Database._add_column(connection, "memory_archive_jobs", "runtime_bundle_id TEXT")
        Database._add_column(connection, "memory_archive_jobs", "root_budget_id TEXT")

    @staticmethod
    def _recover_migration_17(connection: sqlite3.Connection) -> None:
        connection.executescript(MIGRATIONS[16][1])

    @staticmethod
    def _recover_migration_18(connection: sqlite3.Connection) -> None:
        for table, definition in (
            ("conversation_archive_state", "lease_epoch INTEGER NOT NULL DEFAULT 0"),
            ("memory_episodes", "schema_version TEXT NOT NULL DEFAULT 'episode-v1'"),
            ("memory_episodes", "synopsis_json TEXT NOT NULL DEFAULT '[]'"),
            ("memory_episodes", "outcomes_json TEXT NOT NULL DEFAULT '[]'"),
            ("memory_episodes", "source_message_ids_json TEXT NOT NULL DEFAULT '[]'"),
            ("memory_episodes", "source_token_count INTEGER NOT NULL DEFAULT 0"),
            ("memory_episodes", "summary_token_count INTEGER NOT NULL DEFAULT 0"),
            ("memory_episodes", "tokenizer_version TEXT NOT NULL DEFAULT 'utf8-upper-bound-v1'"),
            ("memory_episodes", "deleted_at TEXT"),
            ("memory_context_pins", "binding_hash TEXT NOT NULL DEFAULT ''"),
            ("memory_context_pins", "expires_at TEXT"),
            ("memory_context_pins", "invalidation_reason TEXT"),
        ):
            Database._add_column(connection, table, definition)
        connection.executescript(r"""
        CREATE TABLE IF NOT EXISTS memory_archive_jobs (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
          start_message_seq INTEGER NOT NULL, end_message_seq INTEGER NOT NULL, source_hash TEXT NOT NULL,
          prompt_version TEXT NOT NULL, tokenizer_version TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','RETRY_WAIT','COMPLETED','DEAD_LETTER','LEASE_LOST')),
          available_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
          lease_owner TEXT, lease_epoch INTEGER NOT NULL DEFAULT 0, lease_until TEXT,
          last_error_code TEXT, last_error_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
          UNIQUE(owner_id,thread_id,start_message_seq,end_message_seq,source_hash,prompt_version)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_archive_jobs_claim ON memory_archive_jobs(status,available_at,lease_until,created_at);
        CREATE TABLE IF NOT EXISTS memory_context_pin_items (
          pin_invocation_id TEXT NOT NULL REFERENCES memory_context_pins(model_invocation_id) ON DELETE CASCADE,
          source_type TEXT NOT NULL CHECK(source_type IN ('revision','episode','thread')), source_id TEXT NOT NULL,
          source_version TEXT NOT NULL DEFAULT '', PRIMARY KEY(pin_invocation_id,source_type,source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_context_pin_source ON memory_context_pin_items(source_type,source_id);
        CREATE TABLE IF NOT EXISTS memory_context_pin_payloads (
          pin_invocation_id TEXT PRIMARY KEY REFERENCES memory_context_pins(model_invocation_id) ON DELETE CASCADE,
          rendered TEXT NOT NULL, payload_hash TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_threads_id_owner ON threads(id,owner_id);
        CREATE TRIGGER IF NOT EXISTS memory_episode_scope_insert BEFORE INSERT ON memory_episodes
        WHEN NOT EXISTS (SELECT 1 FROM threads t WHERE t.id=NEW.thread_id AND t.owner_id=NEW.owner_id AND COALESCE(t.project_id,'')=COALESCE(NEW.project_id,''))
        BEGIN SELECT RAISE(ABORT,'memory episode scope mismatch'); END;
        CREATE TRIGGER IF NOT EXISTS memory_episode_scope_update BEFORE UPDATE OF owner_id,thread_id,project_id ON memory_episodes
        WHEN NOT EXISTS (SELECT 1 FROM threads t WHERE t.id=NEW.thread_id AND t.owner_id=NEW.owner_id AND COALESCE(t.project_id,'')=COALESCE(NEW.project_id,''))
        BEGIN SELECT RAISE(ABORT,'memory episode scope mismatch'); END;
        CREATE TRIGGER IF NOT EXISTS archive_state_scope_insert BEFORE INSERT ON conversation_archive_state
        WHEN NOT EXISTS (SELECT 1 FROM threads t WHERE t.id=NEW.thread_id AND t.owner_id=NEW.owner_id)
        BEGIN SELECT RAISE(ABORT,'archive state scope mismatch'); END;
        """)

    @staticmethod
    def _recover_migration_19(connection: sqlite3.Connection) -> None:
        connection.executescript(r"""
        CREATE TABLE IF NOT EXISTS memory_archive_signals (
          turn_id TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
          thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_archive_signals_created ON memory_archive_signals(created_at,turn_id);
        CREATE TRIGGER IF NOT EXISTS memory_archive_signal_on_completed
        AFTER UPDATE OF status ON turns
        WHEN NEW.status='COMPLETED' AND OLD.status<>'COMPLETED'
        BEGIN INSERT OR IGNORE INTO memory_archive_signals(turn_id,thread_id,created_at) VALUES (NEW.id,NEW.thread_id,datetime('now')); END;
        """)

    @staticmethod
    def _recover_migration_20(connection: sqlite3.Connection) -> None:
        """Make memory idempotency keys unique within an owner.

        Older databases encoded these as table-level global UNIQUE constraints.
        SQLite cannot remove such a constraint in place, so rebuild the two
        memory tables while preserving all rows. New databases already use the
        owner-scoped definitions and only need the supporting indexes.
        """
        proposal_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_proposals'"
        ).fetchone()[0]
        audit_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_audit_events'"
        ).fetchone()[0]
        if "UNIQUE(owner_id,request_idempotency_key)" not in proposal_sql:
            connection.execute("ALTER TABLE memory_proposals RENAME TO memory_proposals_v19")
            connection.execute(r"""
            CREATE TABLE memory_proposals (
              id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
              operation TEXT NOT NULL CHECK(operation IN ('ADD','UPDATE','ARCHIVE')),
              target_entry_id TEXT, base_revision_id TEXT,
              kind TEXT NOT NULL CHECK(kind IN ('preference','constraint','fact','decision','lesson')),
              scope_type TEXT NOT NULL CHECK(scope_type IN ('user','project')), scope_id TEXT NOT NULL DEFAULT '',
              content TEXT NOT NULL, fingerprint TEXT NOT NULL,
              evidence_refs_json TEXT NOT NULL DEFAULT '[]', evidence_hash TEXT NOT NULL DEFAULT '',
              origin TEXT NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
              reason TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','ACCEPTED','REJECTED','SUPERSEDED')),
              request_idempotency_key TEXT NOT NULL, decision_idempotency_key TEXT,
              accepted_revision_id TEXT, created_at TEXT NOT NULL, decided_at TEXT,
              UNIQUE(owner_id,request_idempotency_key), UNIQUE(owner_id,decision_idempotency_key)
            )
            """)
            connection.execute(r"""
            INSERT INTO memory_proposals(
              id,owner_id,operation,target_entry_id,base_revision_id,kind,scope_type,scope_id,
              content,fingerprint,evidence_refs_json,evidence_hash,origin,confidence,reason,status,
              request_idempotency_key,decision_idempotency_key,accepted_revision_id,created_at,decided_at
            )
            SELECT id,owner_id,operation,target_entry_id,base_revision_id,kind,scope_type,scope_id,
              content,fingerprint,evidence_refs_json,evidence_hash,origin,confidence,reason,status,
              request_idempotency_key,decision_idempotency_key,accepted_revision_id,created_at,decided_at
            FROM memory_proposals_v19
            """)
            connection.execute("DROP TABLE memory_proposals_v19")
        if "UNIQUE(owner_id,idempotency_key)" not in audit_sql:
            connection.execute("ALTER TABLE memory_audit_events RENAME TO memory_audit_events_v19")
            connection.execute(r"""
            CREATE TABLE memory_audit_events (
              row_id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL, seq INTEGER NOT NULL,
              event_id TEXT NOT NULL UNIQUE, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL, operation TEXT NOT NULL, actor TEXT NOT NULL,
              occurred_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
              UNIQUE(owner_id,seq), UNIQUE(owner_id,idempotency_key)
            )
            """)
            connection.execute(r"""
            INSERT INTO memory_audit_events(
              row_id,owner_id,seq,event_id,aggregate_type,aggregate_id,idempotency_key,
              operation,actor,occurred_at,metadata_json
            )
            SELECT row_id,owner_id,seq,event_id,aggregate_type,aggregate_id,idempotency_key,
              operation,actor,occurred_at,metadata_json
            FROM memory_audit_events_v19
            """)
            connection.execute("DROP TABLE memory_audit_events_v19")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_proposals_owner_request ON memory_proposals(owner_id,request_idempotency_key)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_proposals_owner_decision ON memory_proposals(owner_id,decision_idempotency_key) WHERE decision_idempotency_key IS NOT NULL")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_audit_owner_key ON memory_audit_events(owner_id,idempotency_key)")

    @staticmethod
    def _recover_migration_21(connection: sqlite3.Connection) -> None:
        Database._add_column(connection, "memory_entries", "evidence_state TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED' CHECK(evidence_state IN ('VERIFIED','LEGACY_UNVERIFIED','INVALID'))")
        Database._add_column(connection, "memory_proposals", "request_digest TEXT NOT NULL DEFAULT ''")
        Database._add_column(connection, "memory_proposals", "decision_request_digest TEXT")
        Database._add_column(connection, "memory_proposals", "evidence_state TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED' CHECK(evidence_state IN ('VERIFIED','LEGACY_UNVERIFIED','INVALID'))")
        Database._add_column(connection, "memory_proposals", "version INTEGER NOT NULL DEFAULT 0")
        Database._add_column(connection, "memory_proposals", "accepted_content TEXT")
        Database._add_column(connection, "memory_audit_events", "request_digest TEXT NOT NULL DEFAULT ''")
        connection.executescript(r"""
        CREATE TABLE IF NOT EXISTS memory_evidence_links (
          id TEXT PRIMARY KEY,
          aggregate_type TEXT NOT NULL CHECK(aggregate_type IN ('proposal','revision')),
          aggregate_id TEXT NOT NULL,
          source_type TEXT NOT NULL CHECK(source_type IN ('thread_message','thread_event','run_event')),
          source_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(aggregate_type,aggregate_id,source_type,source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_evidence_aggregate
          ON memory_evidence_links(aggregate_type,aggregate_id,created_at,id);
        UPDATE memory_proposals
          SET status='SUPERSEDED', evidence_state='LEGACY_UNVERIFIED', version=version+1
          WHERE status='PENDING';
        CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_pending_add_fingerprint
          ON memory_proposals(owner_id,scope_type,scope_id,fingerprint)
          WHERE operation='ADD' AND status='PENDING';
        """)

    @staticmethod
    def _recover_migration_22(connection: sqlite3.Connection) -> None:
        Database._add_column(connection, "memory_episodes", "version INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    @contextmanager
    def durable_transaction(self) -> Iterator[sqlite3.Connection]:
        if self.backend == "postgresql":
            with self.transaction() as connection:
                yield connection
            return
        connection = self._connect()
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
