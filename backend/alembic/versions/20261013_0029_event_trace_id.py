"""Promote the event trace id to a queryable column for indexable lookups."""
from alembic import op

revision = "20261013_0029"
down_revision = "20261010_0028"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE events ADD COLUMN trace_id TEXT")
    op.execute("ALTER TABLE thread_events ADD COLUMN trace_id TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_trace_id ON events(trace_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_thread_events_trace_id ON thread_events(trace_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_thread_events_thread_time ON thread_events(thread_id, occurred_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_thread_events_thread_time")
    op.execute("DROP INDEX IF EXISTS idx_thread_events_trace_id")
    op.execute("DROP INDEX IF EXISTS idx_events_trace_id")
    op.execute("ALTER TABLE thread_events DROP COLUMN trace_id")
    op.execute("ALTER TABLE events DROP COLUMN trace_id")
