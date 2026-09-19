"""Scoped memory applicability and cross-asset references."""
from alembic import op
from app.learning_assets import STATEMENTS
revision = "20260910_0008"
down_revision = "20260910_0007"
branch_labels = None
depends_on = None

def upgrade():
    for statement in STATEMENTS:
        op.execute(statement)

def downgrade():
    raise RuntimeError("learning lineage must not be discarded")
