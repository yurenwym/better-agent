"""Fence research attempts independently of their worker name.

Stop old workers before upgrading; do not run mixed worker versions. Existing
leases retain their deadline and are reclaimed with a new epoch after expiry.
"""
from alembic import op

revision = "20261010_0027"
down_revision = "20261009_0026"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE research_jobs ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE turns ADD COLUMN direction_projection_epoch INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE turns ADD COLUMN direction_projection_attempts INTEGER NOT NULL DEFAULT 0")
    for field in ("execution_status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (execution_status IN ('QUEUED','RUNNING'))",
                  "execution_owner TEXT", "execution_until TIMESTAMPTZ",
                  "execution_epoch INTEGER NOT NULL DEFAULT 0", "execution_attempts INTEGER NOT NULL DEFAULT 0"):
        op.execute(f"ALTER TABLE runs ADD COLUMN {field}")


def downgrade():
    # Downgrade is only safe with all workers stopped. Refuse while an attempt
    # could still publish a result under either execution protocol.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM research_jobs WHERE status='RUNNING')
             OR EXISTS (SELECT 1 FROM turns WHERE direction_projection_status='COMPILING')
             OR EXISTS (SELECT 1 FROM runs WHERE execution_status='RUNNING') THEN
            RAISE EXCEPTION 'drain research jobs before removing lease fencing';
          END IF;
        END $$
    """)
    for column in ("execution_attempts", "execution_epoch", "execution_until", "execution_owner", "execution_status"):
        op.execute(f"ALTER TABLE runs DROP COLUMN {column}")
    op.execute("ALTER TABLE turns DROP COLUMN direction_projection_attempts")
    op.execute("ALTER TABLE turns DROP COLUMN direction_projection_epoch")
    op.execute("ALTER TABLE research_jobs DROP COLUMN lease_epoch")
