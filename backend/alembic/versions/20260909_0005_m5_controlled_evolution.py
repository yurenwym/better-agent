"""Add M5 controlled prompt-evolution contracts and paid generation batches."""

from alembic import op


revision = "20260909_0005"
down_revision = "20260909_0004"
branch_labels = None
depends_on = None


def upgrade():
    for statement in (
        "ALTER TABLE evolution_experiences ADD COLUMN IF NOT EXISTS root_task_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE evolution_experiences ADD COLUMN IF NOT EXISTS target_role TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE evolution_experiences ADD COLUMN IF NOT EXISTS provenance TEXT NOT NULL DEFAULT 'production'",
        "ALTER TABLE evolution_experiences ADD COLUMN IF NOT EXISTS source_version TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE evolution_experiences ADD COLUMN IF NOT EXISTS source_state TEXT NOT NULL DEFAULT 'ACTIVE'",
        "ALTER TABLE evolution_experiences ADD COLUMN IF NOT EXISTS runtime_bundle_known INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE evolution_candidates ADD COLUMN IF NOT EXISTS contract_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE evolution_candidates ADD COLUMN IF NOT EXISTS release_contract_version TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE evolution_observer_offsets ADD COLUMN IF NOT EXISTS last_success_row_id BIGINT NOT NULL DEFAULT 0",
        "ALTER TABLE evolution_observer_offsets ADD COLUMN IF NOT EXISTS last_error TEXT",
        "ALTER TABLE evolution_observer_offsets ADD COLUMN IF NOT EXISTS last_error_at TEXT",
        "ALTER TABLE canary_deployments ADD COLUMN IF NOT EXISTS target_role TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE canary_deployments ADD COLUMN IF NOT EXISTS target_purpose TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE canary_deployments ADD COLUMN IF NOT EXISTS budget_microusd BIGINT",
        "ALTER TABLE canary_deployments ADD COLUMN IF NOT EXISTS deadline_at TEXT",
        "ALTER TABLE canary_deployments ADD COLUMN IF NOT EXISTS release_contract_version TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE canary_exposures ADD COLUMN IF NOT EXISTS target_role TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE canary_exposures ADD COLUMN IF NOT EXISTS target_purpose TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE canary_exposures ADD COLUMN IF NOT EXISTS prompt_hit INTEGER",
    ):
        op.execute(statement)
    op.execute("CREATE INDEX IF NOT EXISTS idx_evolution_experience_root ON evolution_experiences(owner_id,root_task_id,created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_evolution_experience_source_state ON evolution_experiences(owner_id,source_state,created_at)")
    op.execute("""
      CREATE TABLE IF NOT EXISTS evolution_content_authorizations (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, subject_id TEXT NOT NULL,
        source_scope_json TEXT NOT NULL, purpose TEXT NOT NULL, expires_at TEXT NOT NULL,
        revoked_at TEXT, created_at TEXT NOT NULL, request_digest TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE
      )
    """)
    op.execute("""
      CREATE TABLE IF NOT EXISTS evolution_generation_batches (
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
      )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_evolution_generation_status ON evolution_generation_batches(owner_id,status,created_at)")


def downgrade():
    raise RuntimeError("M5 evolution audit and authorization history must not be discarded")

