"""Bind M5 release evidence and reserve canary spend before network access."""
from alembic import op

revision = "20260910_0006"
down_revision = "20260909_0005"
branch_labels = None
depends_on = None

STATEMENTS = (
    "ALTER TABLE model_invocations ADD COLUMN system_prompt_digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE canary_deployments ADD COLUMN stable_version BIGINT",
    "ALTER TABLE canary_deployments ADD COLUMN stop_reason TEXT",
    "ALTER TABLE canary_deployments ADD COLUMN max_calls INTEGER",
    "ALTER TABLE canary_exposures ADD COLUMN prompt_digest TEXT",
    "CREATE TABLE evolution_release_suites (digest TEXT PRIMARY KEY, owner_id TEXT NOT NULL, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE canary_attempt_reservations (attempt_id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES canary_deployments(id), amount_microusd BIGINT NOT NULL CHECK(amount_microusd >= 0), created_at TEXT NOT NULL)",
)


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)
    for table in ("evolution_release_suites", "canary_attempt_reservations"):
        op.execute(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()")


def downgrade():
    raise RuntimeError("M5 release and cost evidence must not be discarded")
