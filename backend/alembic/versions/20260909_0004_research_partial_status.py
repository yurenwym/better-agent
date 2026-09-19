"""Add an explicit partial-delivery terminal state for deep research."""

from alembic import op


revision = "20260909_0004"
down_revision = "20260909_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE research_jobs DROP CONSTRAINT research_jobs_status_check")
    op.execute(
        "ALTER TABLE research_jobs ADD CONSTRAINT research_jobs_status_check "
        "CHECK(status IN ('QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED'))"
    )
    op.execute("ALTER TABLE research_jobs DROP CONSTRAINT research_jobs_phase_check")
    op.execute(
        "ALTER TABLE research_jobs ADD CONSTRAINT research_jobs_phase_check "
        "CHECK(phase IN ('queued','planning','retrieving','distilling','reflecting','curating','writing',"
        "'summarizing','finalizing','completed','partial','failed','cancelled'))"
    )


def downgrade():
    raise RuntimeError("partial research delivery history must not be discarded")
