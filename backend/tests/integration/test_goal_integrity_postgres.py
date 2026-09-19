import pytest

from app.db import Database
from test_goal_integrity import (
    scenario,
    test_thirty_days_persist_rest_days_and_restart,
    test_untrusted_compiler_cannot_ignore_rest_days,
    test_deferral_rejects_over_budget_and_rest_day,
    test_concurrent_deferrals_cannot_overfill_one_day,
    test_adjustment_preserves_deferred_task_identity,
    test_completion_feedback_is_atomic_and_note_preserves_metrics,
    test_invalid_atomic_feedback_leaves_action_pending,
    test_late_feedback_requires_new_review_and_keeps_old_evidence,
    test_partial_progress_remains_required_and_survives_reload,
    test_idempotency_cannot_cross_resource_or_operation,
    test_concurrent_same_command_has_one_effect,
    test_partial_deferral_keeps_work_context_and_refreshes_both_dates,
)


@pytest.fixture
def integrity_database(migrated_postgres_url,tmp_path):
    db=Database(migrated_postgres_url,workspace=tmp_path/"artifacts")
    yield db
    db.close()
