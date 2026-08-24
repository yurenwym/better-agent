from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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
 status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED')),
 phase TEXT NOT NULL CHECK(phase IN ('queued','planning','retrieving','distilling','reflecting','curating','writing','summarizing','finalizing','completed','failed','cancelled')),
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
)


class Database:
    def __init__(self, path: str | Path, workspace: str | Path | None = None) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(workspace) if workspace else self.path.parent / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._initialize()

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
                "UPDATE thread_messages SET message_seq=(SELECT COUNT(*) FROM thread_messages prior "
                "WHERE prior.thread_id=thread_messages.thread_id AND (prior.created_at < thread_messages.created_at "
                "OR (prior.created_at=thread_messages.created_at AND prior.id <= thread_messages.id))) WHERE message_seq IS NULL"
            )
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_message_seq ON thread_messages(thread_id,message_seq)")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_message_research ON thread_messages(research_job_id) WHERE research_job_id IS NOT NULL")
            for version, sql in MIGRATIONS:
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                row = connection.execute("SELECT checksum FROM schema_migrations WHERE version=?", (version,)).fetchone()
                if row and row["checksum"] != checksum:
                    raise RuntimeError(f"schema migration {version} checksum mismatch")
                if not row:
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
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
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
    def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    @contextmanager
    def durable_transaction(self) -> Iterator[sqlite3.Connection]:
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
