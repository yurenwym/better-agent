"""Durable learning authority and jobs; no production authority enabled by migration."""
from alembic import op
from app.learning_schema import STATEMENTS

revision = "20260910_0007"
down_revision = "20260910_0006"
branch_labels = None
depends_on = None


def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade():
    raise RuntimeError("learning provenance must not be discarded")
