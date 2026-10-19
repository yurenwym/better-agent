"""SQLite upgrade for notification ownership and delivery status.

PostgreSQL applies the same rules in the Alembic revision. Historical channel
rows stay unowned. A delivery owner is copied only from a live thread row.
"""

SQLITE_NOTIFICATION_UPGRADE = """
ALTER TABLE notification_channels ADD COLUMN owner_id TEXT;

CREATE TABLE notification_deliveries_v2 (
 id TEXT PRIMARY KEY,
 owner_id TEXT,
 job_id TEXT REFERENCES research_jobs(id) ON DELETE CASCADE,
 channel_id TEXT NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
 purpose TEXT NOT NULL CHECK(purpose IN ('report','test')),
 dispatch_key TEXT NOT NULL UNIQUE,
 attempt INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('PENDING','SENDING','SENT','RETRY_WAIT','FAILED','UNKNOWN')),
 http_status INTEGER,
 provider_code TEXT,
 error TEXT,
 error_code TEXT,
 provider_ref TEXT,
 created_at TEXT NOT NULL,
 claimed_at TEXT,
 finished_at TEXT
);

INSERT INTO notification_deliveries_v2(
 id, owner_id, job_id, channel_id, purpose, dispatch_key, attempt, status,
 http_status, provider_code, error, error_code, provider_ref, created_at, claimed_at, finished_at)
SELECT d.id,
 (SELECT t.owner_id FROM research_jobs j JOIN threads t ON t.id=j.thread_id
  WHERE j.id=d.job_id AND t.deleted_at IS NULL AND t.owner_id IS NOT NULL AND trim(t.owner_id)<>''),
 d.job_id, d.channel_id, 'report', 'legacy:' || d.id, d.attempt,
 CASE d.status WHEN 'PENDING' THEN 'UNKNOWN' ELSE d.status END,
 d.http_status, d.provider_code, d.error, NULL, NULL, d.created_at, NULL, d.finished_at
FROM notification_deliveries d;

DROP TABLE notification_deliveries;
ALTER TABLE notification_deliveries_v2 RENAME TO notification_deliveries;
"""
