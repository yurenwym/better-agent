"""Append-only V3 learning promotion audit."""
from alembic import op
from app.learning_v3_schema import PROMOTION_POSTGRES_TRIGGERS, PROMOTION_TABLE_STATEMENTS

revision = "20260921_0022"
down_revision = "20260921_0021"
branch_labels = None
depends_on = None


def upgrade():
    for statement in PROMOTION_TABLE_STATEMENTS:
        op.execute(statement)
    for statement in PROMOTION_POSTGRES_TRIGGERS:
        op.execute(statement)


def downgrade():
    raise RuntimeError("learning promotion audit must not be discarded")
