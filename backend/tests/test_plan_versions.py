import pytest


def test_plan_revision_creates_immutable_version_and_preserves_completed_steps(tmp_path) -> None:
    from app.db import Database
    from app.domain import PlanConflict, PlanVersionService

    service = PlanVersionService(Database(tmp_path / "agent.db"))
    first = service.create(
        "run-1",
        "goal-1",
        [{"id": "step-a", "title": "A"}, {"id": "step-b", "title": "B"}],
    )
    service.mark_step_completed(first.id, "step-a")

    second = service.revise(
        "run-1",
        "goal-1",
        expected_version=1,
        steps=[
            {"id": "step-a", "title": "A changed"},
            {"id": "step-c", "title": "C"},
        ],
    )

    assert first.version == 1
    assert second.version == 2
    assert service.get(first.id).steps[0].title == "A"
    assert service.get(second.id).steps[0].status == "completed"
    assert [step.id for step in second.steps] == ["step-a", "step-c"]

    with pytest.raises(PlanConflict):
        service.revise("run-1", "goal-1", expected_version=1, steps=[])


def test_plan_approval_only_allows_current_version(tmp_path) -> None:
    from app.db import Database
    from app.domain import PlanConflict, PlanVersionService

    service = PlanVersionService(Database(tmp_path / "agent.db"))
    first = service.create("run-1", "goal-1", [{"id": "step-a", "title": "A"}])
    second = service.revise("run-1", "goal-1", 1, [{"id": "step-b", "title": "B"}])

    with pytest.raises(PlanConflict):
        service.approve(first.id)

    approved = service.approve(second.id)
    assert approved.status == "approved"


def test_manual_plan_revision_inherits_the_document_source_version(tmp_path) -> None:
    from app.db import Database
    from app.domain import PlanVersionService

    service = PlanVersionService(Database(tmp_path / "agent.db"))
    first = service.create(
        "run-1",
        "goal-1",
        [{"id": "step-a", "title": "A"}],
        source_document_version_id="planv-document-1",
    )

    revised = service.revise("run-1", "goal-1", 1, [{"id": "step-a", "title": "A revised"}])

    assert first.source_document_version_id == "planv-document-1"
    assert revised.source_document_version_id == "planv-document-1"

