"""Keep completed reviews independent from adjustment generation failures."""
from alembic import op

revision = "20260912_0013"
down_revision = "20260912_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for definition in (
        "adjustment_status TEXT NOT NULL DEFAULT 'NOT_NEEDED'",
        "adjustment_error_code TEXT",
        "adjustment_attempts INTEGER NOT NULL DEFAULT 0",
    ):
        op.execute(f"ALTER TABLE goal_daily_reviews ADD COLUMN {definition}")
    op.execute("UPDATE goal_daily_reviews SET adjustment_status='COMPLETED' WHERE proposal_id IS NOT NULL")


def downgrade() -> None:
    raise RuntimeError("completed review and adjustment state cannot be discarded")
