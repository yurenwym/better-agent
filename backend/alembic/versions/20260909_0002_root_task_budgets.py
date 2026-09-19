"""Expand the authoritative schema for shared root task budgets."""
from alembic import op

revision = "20260909_0002"
down_revision = "20260905_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE cost_budgets DROP CONSTRAINT cost_budgets_period_kind_check")
    op.execute("ALTER TABLE cost_budgets ADD CONSTRAINT cost_budgets_period_kind_check "
               "CHECK(period_kind IN ('INVOCATION','DAILY','MONTHLY','ROOT'))")
    op.execute("""CREATE TABLE task_budget_roots (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        root_kind TEXT NOT NULL, root_object_id TEXT NOT NULL,
        max_attempts BIGINT NOT NULL CHECK(max_attempts >= 0),
        attempts_started BIGINT NOT NULL DEFAULT 0 CHECK(attempts_started >= 0),
        deadline_at TIMESTAMPTZ NOT NULL,
        version BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(owner_id,root_kind,root_object_id), UNIQUE(owner_id,id),
        CHECK(attempts_started <= max_attempts)
    )""")
    for table in ("turns", "runs", "agent_runs", "research_jobs", "evaluation_runs", "model_invocations"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN root_budget_id TEXT REFERENCES task_budget_roots(id)")
        op.execute(f"CREATE INDEX idx_{table}_root_budget ON {table}(root_budget_id)")
    op.execute("ALTER TABLE model_invocations ADD CONSTRAINT model_invocations_root_owner_fk "
               "FOREIGN KEY(owner_id,root_budget_id) REFERENCES task_budget_roots(owner_id,id)")


def downgrade():
    raise RuntimeError("root budget history must not be discarded by a schema downgrade")
