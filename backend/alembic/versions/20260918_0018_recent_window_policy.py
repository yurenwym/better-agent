"""Persist the R2-02 recent-window ceiling on immutable profile versions.

``hot_window`` derives ``R`` from ``recent_window_bytes`` and
``recent_window_ratio``, but those fields had no column, so a profile loaded
from the database could only ever carry the documented defaults - the same gap
``20260918_0016`` closed for ``history_min_turns`` and ``compact_ratio``. Both
columns stay NULL for existing versions, which keeps the defaults.
"""

from alembic import op

revision = "20260918_0018"
down_revision = "20260918_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE model_profile_versions ADD COLUMN recent_window_bytes INTEGER")
    op.execute("ALTER TABLE model_profile_versions ADD COLUMN recent_window_ratio REAL")


def downgrade() -> None:
    raise RuntimeError("frozen model profile versions cannot be rewritten")
