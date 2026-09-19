"""Shared DDL for PostgreSQL authority and SQLite contract fixtures."""
STATEMENTS = (
    """CREATE TABLE learning_policies (
        owner_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1,
        paused INTEGER NOT NULL DEFAULT 1 CHECK(paused IN (0,1)),
        config_json TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE learning_jobs (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_hash TEXT NOT NULL,
        root_id TEXT NOT NULL, provenance TEXT NOT NULL DEFAULT 'production',
        policy_version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'QUEUED'
            CHECK(status IN ('QUEUED','RUNNING','UNKNOWN','NO_CHANGE','APPLIED','REJECTED','SUSPENDED')),
        version INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until TEXT,
        dispatched_at TEXT, root_budget_id TEXT, checkpoint_json TEXT NOT NULL DEFAULT '{}',
        change_set_json TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(owner_id,source_kind,source_id,source_hash)
    )""",
    "CREATE INDEX idx_learning_jobs_claim ON learning_jobs(owner_id,status,lease_until,created_at)",
)
