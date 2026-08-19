from __future__ import annotations

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
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    active_turn_id TEXT,
    next_event_seq INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    materialized_goal_id TEXT,
    materialized_run_id TEXT,
    direction_action TEXT,
    direction_idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(thread_id, client_turn_id)
);
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
            turn_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(turns)").fetchall()
            }
            if "skill_names_json" not in turn_columns:
                connection.execute("ALTER TABLE turns ADD COLUMN skill_names_json TEXT NOT NULL DEFAULT '[]'")
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
