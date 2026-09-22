"""Append-only V3 learning decision audit."""
from alembic import op
from app.learning_v3_schema import POSTGRES_TRIGGERS, TABLE_STATEMENTS

revision = "20260921_0021"
down_revision = "20260920_0020"
branch_labels = None
depends_on = None


def upgrade():
    for statement in TABLE_STATEMENTS:
        op.execute(statement)
    for statement in POSTGRES_TRIGGERS:
        op.execute(statement)


def downgrade():
    raise RuntimeError("learning decision audit must not be discarded")
