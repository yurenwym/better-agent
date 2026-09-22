"""DDL for the V3 learning audit tables.

Both `learning_decisions` and `learning_promotions` are append-only: they are
evidence of what the harness decided and applied at a point in time, so neither
the application nor a migration may rewrite them. PostgreSQL reuses the shared
`reject_append_only_mutation()` function; SQLite gets an equivalent trigger.
"""

TABLE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE learning_decisions (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        learn INTEGER NOT NULL CHECK(learn IN (0,1)),
        target TEXT NOT NULL CHECK(target IN ('MEMORY','SKILL','BEHAVIOR','IGNORE')),
        subtype TEXT NOT NULL DEFAULT '' CHECK(subtype IN ('','prompt','task_policy','policy','reasoning_policy','model_policy')),
        confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
        importance REAL NOT NULL CHECK(importance >= 0 AND importance <= 1),
        risk TEXT NOT NULL CHECK(risk IN ('low','medium','high')),
        reason_codes_json TEXT NOT NULL DEFAULT '[]',
        input_digest TEXT NOT NULL,
        decision_digest TEXT NOT NULL,
        model_identity TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK((learn = 1 AND target <> 'IGNORE') OR (learn = 0 AND target = 'IGNORE')),
        CHECK(target = 'BEHAVIOR' OR subtype = ''),
        UNIQUE(owner_id, job_id)
    )""",
    "CREATE INDEX idx_learning_decisions_owner ON learning_decisions(owner_id, created_at, id)",
    "CREATE INDEX idx_learning_decisions_target ON learning_decisions(target, created_at)",
)

SQLITE_TRIGGERS: tuple[str, ...] = (
    "CREATE TRIGGER learning_decisions_append_only BEFORE UPDATE ON learning_decisions "
    "BEGIN SELECT RAISE(ABORT,'learning_decisions is append-only'); END",
    "CREATE TRIGGER learning_decisions_no_delete BEFORE DELETE ON learning_decisions "
    "BEGIN SELECT RAISE(ABORT,'learning_decisions is append-only'); END",
)

POSTGRES_TRIGGERS: tuple[str, ...] = (
    "CREATE TRIGGER learning_decisions_append_only BEFORE UPDATE OR DELETE ON learning_decisions "
    "FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()",
)

# `learning_promotions` is the promotion audit trail (V3 §53 observability, §60
# "必须可审计"). The partial unique index is the hard form of V3 §61
# "Duplicate promotion = 0": the database itself refuses a second PROMOTED row
# for the same candidate, no matter what the application layer does.
PROMOTION_TABLE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE learning_promotions (
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL,
        job_id TEXT NOT NULL DEFAULT '',
        target TEXT NOT NULL CHECK(target IN ('MEMORY','SKILL','BEHAVIOR')),
        candidate_id TEXT NOT NULL,
        evaluation_id TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL CHECK(outcome IN (
            'PROMOTED','CANARY_STARTED','CANARY_PENDING','PENDING_APPROVAL',
            'PENDING_RELEASE_EVALUATION','NEEDS_REPLAY','REJECTED','ROLLED_BACK','SHADOW')),
        gate_json TEXT NOT NULL DEFAULT '{}',
        reason TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        evidence_digest TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(owner_id, idempotency_key)
    )""",
    "CREATE INDEX idx_learning_promotions_owner ON learning_promotions(owner_id, created_at, id)",
    "CREATE INDEX idx_learning_promotions_candidate ON learning_promotions(candidate_id, created_at)",
    "CREATE UNIQUE INDEX idx_learning_promotions_single_promotion "
    "ON learning_promotions(owner_id, candidate_id) WHERE outcome = 'PROMOTED'",
)

PROMOTION_SQLITE_TRIGGERS: tuple[str, ...] = (
    "CREATE TRIGGER learning_promotions_append_only BEFORE UPDATE ON learning_promotions "
    "BEGIN SELECT RAISE(ABORT,'learning_promotions is append-only'); END",
    "CREATE TRIGGER learning_promotions_no_delete BEFORE DELETE ON learning_promotions "
    "BEGIN SELECT RAISE(ABORT,'learning_promotions is append-only'); END",
)

PROMOTION_POSTGRES_TRIGGERS: tuple[str, ...] = (
    "CREATE TRIGGER learning_promotions_append_only BEFORE UPDATE OR DELETE ON learning_promotions "
    "FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()",
)
