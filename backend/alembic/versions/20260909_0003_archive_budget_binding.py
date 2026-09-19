"""Bind archive model work to an immutable bundle and root task budget."""

from alembic import op


revision = "20260909_0003"
down_revision = "20260909_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE memory_archive_jobs ADD COLUMN runtime_bundle_id TEXT "
        "REFERENCES runtime_bundles(id)"
    )
    op.execute(
        "ALTER TABLE memory_archive_jobs ADD COLUMN root_budget_id TEXT "
        "REFERENCES task_budget_roots(id)"
    )
    op.execute(
        "ALTER TABLE memory_archive_jobs ADD CONSTRAINT memory_archive_jobs_root_owner_fk "
        "FOREIGN KEY(owner_id,root_budget_id) REFERENCES task_budget_roots(owner_id,id)"
    )
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_root_owner_fk "
        "FOREIGN KEY(owner_id,root_budget_id) REFERENCES task_budget_roots(owner_id,id)"
    )
    op.execute(
        "ALTER TABLE evaluation_runs ADD CONSTRAINT evaluation_runs_root_owner_fk "
        "FOREIGN KEY(owner_id,root_budget_id) REFERENCES task_budget_roots(owner_id,id)"
    )
    op.execute(
        "CREATE INDEX idx_memory_archive_jobs_root_budget "
        "ON memory_archive_jobs(root_budget_id)"
    )


def downgrade():
    raise RuntimeError("archive budget binding history must not be discarded")
