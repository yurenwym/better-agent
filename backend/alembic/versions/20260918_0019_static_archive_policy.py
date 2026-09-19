"""Persist the R2-03 static early-archival policy.

Two independent gaps are closed here.

``model_profile_versions`` gains the three R2-03 knobs. ``hot_window`` derives
``G``/``B``/``D``/``T`` from them, but with no column a profile loaded from the
database could only ever carry the documented 30%/20% defaults - the same gap
``20260918_0016`` closed for ``history_min_turns``/``compact_ratio`` and
``20260918_0018`` for the recent-window ceiling.

``memory_archive_jobs`` gains the budget version the job was created under. The
plan requires each job to state its source scope *and* its budget version; the
source scope was already pinned by start/end sequence plus ``source_hash``, but
a job could not say which policy produced it, so no pass was auditable. Both
columns stay NULL for existing rows, which is accurate: they predate the static
line and were created by the foreground hard-cap path.
"""

from alembic import op

revision = "20260918_0019"
down_revision = "20260918_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE model_profile_versions ADD COLUMN archive_trigger_ratio REAL")
    op.execute("ALTER TABLE model_profile_versions ADD COLUMN archive_reserve_ratio REAL")
    op.execute("ALTER TABLE model_profile_versions ADD COLUMN archive_prefix_reserve INTEGER")
    op.execute("ALTER TABLE memory_archive_jobs ADD COLUMN budget_policy_version TEXT")
    op.execute("ALTER TABLE memory_archive_jobs ADD COLUMN budget_profile_version_id TEXT")


def downgrade() -> None:
    raise RuntimeError("frozen model profile versions cannot be rewritten")
