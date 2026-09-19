"""Persist the R1 model budget contract on immutable profile versions.

``validation_tier`` stays NULL for every pre-existing version, which keeps the
historical ``C - O - margin`` formula. A version that declares tier ``A`` must
also carry an evidenced ``admitted_context_limit`` and
``counter_evidence_version``; the repository default window is not capacity
evidence, so those columns are intentionally nullable rather than backfilled.
"""

from alembic import op

revision = "20260918_0014"
down_revision = "20260912_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for definition in (
        "admitted_context_limit INTEGER",
        "soft_context_limit INTEGER",
        "context_window_verified INTEGER NOT NULL DEFAULT 0",
        "validation_tier TEXT",
        "counter_id TEXT NOT NULL DEFAULT 'utf8-upper-bound'",
        "counter_version TEXT NOT NULL DEFAULT 'utf8-upper-bound-v1'",
        "counter_evidence_version TEXT",
        "capacity_evidence TEXT",
        "protocol_budget_json TEXT NOT NULL DEFAULT '{}'",
    ):
        op.execute(f"ALTER TABLE model_profile_versions ADD COLUMN {definition}")


def downgrade() -> None:
    raise RuntimeError("frozen model profile versions cannot be rewritten")
