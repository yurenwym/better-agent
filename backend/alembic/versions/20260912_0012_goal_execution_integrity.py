"""Persist confirmed calendars, partial progress and review evidence revisions."""
from alembic import op

revision = "20260912_0012"
down_revision = "20260912_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, definition in (
        ("goal_programs", "schedule_constraints_json TEXT NOT NULL DEFAULT '{}'"),
        ("goal_actions", "progress_json TEXT NOT NULL DEFAULT '{}'"),
        ("goal_action_feedback", "details_json TEXT NOT NULL DEFAULT '{}'"),
        ("goal_daily_reviews", "evidence_stale INTEGER NOT NULL DEFAULT 0"),
        ("goal_daily_reviews", "revision INTEGER NOT NULL DEFAULT 0"),
        ("goal_daily_reviews", "history_json TEXT NOT NULL DEFAULT '[]'"),
        ("goal_review_action_snapshots", "feedback_json TEXT NOT NULL DEFAULT '{}'"),
    ):
        op.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def downgrade() -> None:
    raise RuntimeError("execution progress and review history cannot be discarded")
