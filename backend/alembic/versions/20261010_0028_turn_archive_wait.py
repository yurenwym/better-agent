"""Persist turn dependencies on history archival; stop workers before upgrade."""
from alembic import op

revision = "20261010_0028"
down_revision = "20261010_0027"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE turn_jobs ADD COLUMN archive_job_id TEXT REFERENCES memory_archive_jobs(id)")
    op.execute("ALTER TABLE turn_jobs ADD COLUMN archive_wait_until TIMESTAMPTZ")


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM turn_jobs WHERE archive_job_id IS NOT NULL AND status='QUEUED') THEN
            RAISE EXCEPTION 'drain archive-dependent turns before downgrade';
        END IF;
    END $$""")
    op.execute("ALTER TABLE turn_jobs DROP COLUMN archive_wait_until")
    op.execute("ALTER TABLE turn_jobs DROP COLUMN archive_job_id")
