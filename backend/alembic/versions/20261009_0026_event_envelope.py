"""Add optional event metadata without rewriting historical events."""
from alembic import op

revision = "20261009_0026"
down_revision = "20260930_0025"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE events ADD COLUMN envelope_json TEXT")
    op.execute("ALTER TABLE thread_events ADD COLUMN envelope_json TEXT")


def downgrade():
    op.execute("ALTER TABLE thread_events DROP COLUMN envelope_json")
    op.execute("ALTER TABLE events DROP COLUMN envelope_json")
