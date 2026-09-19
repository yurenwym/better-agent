"""Persist the R1 selection policy on immutable profile versions.

``hot_window`` reads ``history_min_turns`` and ``compact_ratio`` from the
routed profile, but those fields had no column, so a profile loaded from the
database could only ever carry the defaults. That made "the policy comes from
the versioned profile" untrue in practice. Both columns stay NULL for existing
versions, which keeps the documented defaults.
"""

from alembic import op

revision = "20260918_0016"
down_revision = "20260918_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE model_profile_versions ADD COLUMN history_min_turns INTEGER")
    op.execute("ALTER TABLE model_profile_versions ADD COLUMN compact_ratio REAL")


def downgrade() -> None:
    raise RuntimeError("frozen model profile versions cannot be rewritten")
