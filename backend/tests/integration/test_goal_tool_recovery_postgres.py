import pytest
from app.db import Database
from test_goal_tool_recovery import (
    test_travel_delivery_recovers_without_creating_execution,
    test_preview_is_not_user_consent,
    test_activation_recovers_commit_before_tool_result,
    test_changed_schedule_invalidates_approval,
    test_draft_operation_rejects_changed_content,
    test_concurrent_draft_creation_has_one_version,
    test_project_scope_blocks_reads_and_writes,
)


@pytest.fixture
def recovery_db(migrated_postgres_url, tmp_path):
    db = Database(migrated_postgres_url, workspace=tmp_path / "artifacts")
    yield db
    db.close()
