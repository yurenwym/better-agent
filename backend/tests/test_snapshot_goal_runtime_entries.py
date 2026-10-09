"""T33/T36/T37: the goal Runtime executes under an authorized owner.

``/api/goals`` creates runs that have no source turn.  Those runs used to be the
reason ``AgentRuntime`` could not name an owner for its model calls; these tests
drive the real ``create_goal`` / ``handle_message`` / ``_finish_exposure`` path
through the routed gateway, so a regression surfaces as a committed-row
assertion rather than as a helper that set its own context.
"""
from __future__ import annotations

import pytest

from test_snapshot_flow import _plane
from test_snapshot_gateway import _answer


def _runtime(tmp_path, db, gateway, *, owner_id: str | None = None):
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.live_model import LiveRuntimeModel
    from app.memory import MemoryService
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry

    events = EventStore(db)
    approvals = ApprovalService(db)
    extra = {} if owner_id is None else {"owner_id": owner_id}
    return AgentRuntime(
        db=db, events=events, plans=PlanVersionService(db), approvals=approvals,
        checkpoints=CheckpointStore(db), memory=MemoryService(db, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals),
        model=LiveRuntimeModel(gateway), **extra,
    )


def _invocations(db):
    with db.connection() as connection:
        return connection.execute(
            "SELECT owner_id,purpose,run_id,context_snapshot_id FROM model_invocations ORDER BY created_at,id",
        ).fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "tenant-b"])
async def test_t33_a_goal_api_run_executes_under_the_service_owner(tmp_path, monkeypatch, owner) -> None:
    async def execute(_profile, _request, **_kwargs):
        return _answer('{"summary":"计划","steps":[{"id":"s1","title":"开始","description":""}]}')

    db, _bundle, _, _, gateway = _plane(tmp_path, monkeypatch, execute=execute, owner_id=owner or "local-user")
    runtime = _runtime(tmp_path, db, gateway, owner_id=owner)

    run = await runtime.create_goal("目标", "描述")
    assert run.source_turn_id is None
    # The real Runtime entry (``_model_call``), not a context the test set itself.
    draft = await runtime._model_call(run, "planning", runtime.model.plan, runtime._goal(run.goal_id), [])

    assert draft is not None and len(draft.steps) == 1
    rows = _invocations(db)
    assert [row["purpose"] for row in rows] == ["planning"]
    assert rows[0]["owner_id"] == (owner or "local-user")
    assert rows[0]["run_id"] == run.id
    assert rows[0]["context_snapshot_id"]


@pytest.mark.asyncio
async def test_t37_a_run_whose_source_turn_has_no_owner_sends_nothing(tmp_path, monkeypatch) -> None:
    sent: list[object] = []

    async def execute(_profile, request, **_kwargs):
        sent.append(request)
        return _answer('{"needs_clarification": false}')

    db, _bundle, _, _, gateway = _plane(tmp_path, monkeypatch, execute=execute)
    runtime = _runtime(tmp_path, db, gateway)
    run = await runtime.create_goal("目标", "描述")
    # A dangling source turn is an identity we cannot authorize.
    with db.transaction() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE runs SET source_turn_id='turn-that-does-not-exist' WHERE id=?", (run.id,))

    with pytest.raises(PermissionError):
        await runtime.handle_message(run.id, "开始")

    assert sent == []
    assert _invocations(db) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "tenant-b"])
async def test_t36_the_run_output_judge_is_an_independent_call_under_the_run_owner(
    tmp_path, monkeypatch, owner,
) -> None:
    from app.evolution import LiveSafetyJudge

    async def execute(_profile, _request, **_kwargs):
        return _answer("safe")

    db, _bundle, _, _, gateway = _plane(tmp_path, monkeypatch, execute=execute, owner_id=owner or "local-user")
    runtime = _runtime(tmp_path, db, gateway, owner_id=owner)
    verdicts: list[object] = []

    class Evolution:
        def finish_run_exposure(self, exposure_id, *, success, safety_pass):
            verdicts.append(safety_pass)

    run = await runtime.create_goal("目标", "描述")
    runtime.evolution = Evolution()
    runtime.safety_judge = LiveSafetyJudge(gateway)
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO messages(id,run_id,role,content,created_at) VALUES ('m1',?,'assistant','答案','2026-10-08T00:00:00+00:00')",
            (run.id,),
        )

    await runtime._finish_exposure(run.id, success=True)

    assert verdicts == [True]
    rows = _invocations(db)
    assert [row["purpose"] for row in rows] == ["judge_run_output"]
    assert rows[0]["owner_id"] == (owner or "local-user")
    assert rows[0]["context_snapshot_id"]


@pytest.mark.asyncio
async def test_t37_a_run_output_judge_without_an_owner_is_not_sent(tmp_path, monkeypatch) -> None:
    from app.evolution import LiveSafetyJudge

    sent: list[object] = []

    async def execute(_profile, request, **_kwargs):
        sent.append(request)
        return _answer("safe")

    db, _bundle, _, _, gateway = _plane(tmp_path, monkeypatch, execute=execute)
    runtime = _runtime(tmp_path, db, gateway)
    verdicts: list[object] = []

    class Evolution:
        def finish_run_exposure(self, exposure_id, *, success, safety_pass):
            verdicts.append(safety_pass)

    run = await runtime.create_goal("目标", "描述")
    runtime.evolution = Evolution()
    runtime.safety_judge = LiveSafetyJudge(gateway)
    with db.transaction() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("UPDATE runs SET source_turn_id='turn-that-does-not-exist' WHERE id=?", (run.id,))
        connection.execute(
            "INSERT INTO messages(id,run_id,role,content,created_at) VALUES ('m1',?,'assistant','答案','2026-10-08T00:00:00+00:00')",
            (run.id,),
        )

    await runtime._finish_exposure(run.id, success=True)

    assert sent == []
    assert verdicts == [None]


def test_the_runtime_service_owner_must_be_explicit_and_non_empty(tmp_path) -> None:
    from app.db import Database
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.memory import MemoryService
    from app.runtime import AgentRuntime, MockModelGateway
    from app.tools import create_default_registry

    db = Database(tmp_path / "agent.db", workspace=tmp_path / "workspace")
    events = EventStore(db)
    approvals = ApprovalService(db)
    with pytest.raises(ValueError):
        AgentRuntime(
            db=db, events=events, plans=PlanVersionService(db), approvals=approvals,
            checkpoints=CheckpointStore(db), memory=MemoryService(db, events, tmp_path / "memory"),
            tools=create_default_registry(tmp_path / "workspace", db=db, approval_service=approvals),
            model=MockModelGateway(), owner_id="  ",
        )


# --- Review-fix regressions: explicit owners and the explicit offline path -------------


@pytest.mark.parametrize("owner", ["", "  ", None])
def test_expert_advisory_start_refuses_a_missing_owner_before_any_row(tmp_path, owner) -> None:
    from app.agents import AgentTaskService, ExpertAdvisoryService
    from app.behavior import BehaviorBundleService
    from app.db import Database

    db = Database(tmp_path / "agent.db")
    bundles = BehaviorBundleService(db)
    bundle = bundles.ensure({"prompt": "v1"})
    bundles.activate("stable", bundle.id, "stable")
    advisor = ExpertAdvisoryService(AgentTaskService(db), bundles)

    with pytest.raises(PermissionError, match="authorized owner"):
        advisor.start(purpose="plan", source_id="s", objective="o", context={}, roles=("planner",), owner_id=owner)
    with db.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_evolution_proposer_without_an_owner_sends_nothing(tmp_path, monkeypatch) -> None:
    from app.evolution import LivePromptCandidateProposer

    sent: list[object] = []

    async def execute(request):
        sent.append(request)
        return _answer("{}")

    db, _bundle, _, _, gateway = _plane(tmp_path, monkeypatch, execute=execute)
    proposer = LivePromptCandidateProposer(gateway)

    with pytest.raises(PermissionError, match="owner is required"):
        await proposer._propose("prompt", {}, "bundle")
    assert sent == [] and _invocations(db) == []


@pytest.mark.asyncio
async def test_unbound_gateway_needs_an_explicit_offline_opt_in() -> None:
    from app.model_gateway import GatewayError, ModelGateway, ModelProfile, ModelRequest

    profile = ModelProfile("https://example.invalid/v1", "m", "KEY", provider_name="openai-compatible", max_attempts=1)
    with pytest.raises(GatewayError, match="offline") as refused:
        await ModelGateway(profile).complete(ModelRequest(messages=[], purpose="x"))
    assert refused.value.kind == "configuration"

    opted_in = ModelGateway(profile, offline_unbound=True)
    assert opted_in.offline_unbound is True


@pytest.mark.asyncio
async def test_t34_projection_claim_failure_and_ready_retry_keep_the_snapshot(tmp_path, monkeypatch):
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport
    from test_plan_execution_projection import ExecutionModel

    db, bundle, _, _, gateway = _plane(tmp_path, monkeypatch, execute=None, owner_id="tenant-b")
    recorder = CommittedSnapshotTransport(db, "tenant-b", "CS-GR-03", _answer(
        '{"summary":"Plan","steps":[{"id":"s1","title":"Track","description":""}]}'
    ))
    monkeypatch.setattr(gateway, "_execute_attempt", recorder)
    runtime = _runtime(tmp_path, db, gateway)
    runtime.conversation.route_model = ExecutionModel()
    thread = runtime.conversation.create_thread("Projection", owner_id="tenant-b")
    runtime.plan_documents.save_model_revision(
        thread_id=thread.id, title="Plan", markdown_content="# Plan\nTrack daily",
        source_turn_id=None, source_message_id=None, actor="user",
    )
    accepted = runtime.conversation.accept_turn(thread.id, "projection", "Track this", [], owner_id="tenant-b")
    await runtime.turn_worker.run_once()
    turn = runtime.conversation.turn(accepted.turn_id, owner_id="tenant-b")
    materializer = runtime.conversation.materializer
    claim = materializer._claim_projection
    store = materializer._store_projection_draft

    def refused(*args):
        raise RuntimeError("claim unavailable")

    monkeypatch.setattr(materializer, "_claim_projection", refused)
    with pytest.raises(RuntimeError, match="claim unavailable"):
        await materializer.materialize(turn.id, turn.version, "project", "continue_execution")
    assert recorder.observations == [] and _invocations(db) == []
    monkeypatch.setattr(materializer, "_claim_projection", claim)

    def interrupted(*args):
        store(*args)
        raise RuntimeError("interrupted after READY")

    monkeypatch.setattr(materializer, "_store_projection_draft", interrupted)
    with pytest.raises(RuntimeError, match="after READY"):
        await materializer.materialize(turn.id, turn.version, "project", "continue_execution")
    assert len(recorder.observations) == 1
    frozen = ModelInputSnapshotStore(db).load("tenant-b", recorder.observations[0]["snapshot_id"])
    assert gateway.current_call_context() is None
    monkeypatch.setattr(materializer, "_store_projection_draft", store)
    result = await materializer.materialize(turn.id, turn.version, "project", "continue_execution")
    replay = await materializer.materialize(turn.id, turn.version, "project", "continue_execution")
    assert result.run_id == replay.run_id
    assert len(recorder.observations) == 1
    assert ModelInputSnapshotStore(db).load("tenant-b", frozen.id).content_json == frozen.content_json
    with db.connection() as connection:
        row = connection.execute("SELECT * FROM model_invocations").fetchone()
        pinned = connection.execute("SELECT * FROM turns WHERE id=?", (turn.id,)).fetchone()
    assert row["owner_id"] == "tenant-b" and row["turn_id"] == turn.id
    assert row["runtime_bundle_id"] == pinned["runtime_bundle_id"] == bundle.id
    assert row["root_budget_id"] == pinned["root_budget_id"]


def test_t35_daily_and_period_review_restore_context_after_failure(tmp_path, monkeypatch):
    import asyncio
    from app.goal_program_compiler import GoalProgramCompiler
    from app.model_control import ModelCallContext
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport
    from test_goal_programs import preview
    from test_goal_reviews import review_services

    monkeypatch.setattr("app.goal_reviews._local_date", lambda _: "2026-09-01")
    db, goals, version, _, reviews, worker = review_services(tmp_path, needs_adjustment=False)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    _, _, _, _, gateway = _plane(tmp_path, monkeypatch, execute=None, database=db)
    compiler = GoalProgramCompiler(gateway)
    goals.compiler = reviews.compiler = compiler
    recorder = CommittedSnapshotTransport(db, "local-user", "CS-GP-01", [
        RuntimeError("provider interrupted"), _answer("not JSON"),
        _answer('{"summary":"Done","encouragement":"Continue","needs_adjustment":false,"adjustment_reason":""}'),
        _answer('{"summary":"Period complete"}'),
    ])
    monkeypatch.setattr(gateway, "_execute_attempt", recorder)
    goals.complete_action(active["actions"][0]["id"], expected_version=0, idempotency_key="done")
    goals.close_day(active["id"], "2026-09-01", idempotency_key="close")

    async def exercise():
        sentinel = ModelCallContext(owner_id="unrelated-owner", role="reflector", purpose="unrelated")
        token = gateway.set_call_context(sentinel)
        try:
            assert await worker.run_once()
            assert gateway.current_call_context() is sentinel
            assert reviews.for_program_date(active["id"], "2026-09-01")["status"] == "QUEUED"
            assert await worker.run_once()
            assert gateway.current_call_context() is sentinel
            assert reviews.for_program_date(active["id"], "2026-09-01")["status"] == "COMPLETED"
            for action in active["actions"][1:]:
                goals.complete_action(action["id"], expected_version=0, idempotency_key=action["id"])
            current = goals.get(active["id"])
            goals.transition(active["id"], "complete", expected_version=current["version"], idempotency_key="finish")
            assert await goals.period_summary(active["id"]) == "Period complete"
            assert gateway.current_call_context() is sentinel
        finally:
            gateway.reset_call_context(token)

    asyncio.run(exercise())
    assert len(recorder.observations) == 4
    assert len({item["snapshot_id"] for item in recorder.observations}) == 4
    first, repair = [ModelInputSnapshotStore(db).load("local-user", item["snapshot_id"])
                     for item in recorder.observations[1:3]]
    assert len(first.to_request().messages) == 2
    assert len(repair.to_request().messages) == 4
    rows = _invocations(db)
    assert [row["purpose"] for row in rows] == ["daily_review"] * 3 + ["period_review"]
    assert all(row["owner_id"] == "local-user" for row in rows)


def test_t35_adjustment_api_repair_has_independent_committed_snapshots(tmp_path, monkeypatch):
    import json
    from app.goal_program_compiler import GoalProgramCompiler
    from app.model_input_snapshot_store import ModelInputSnapshotStore
    from snapshot_entrypoint_helpers import CommittedSnapshotTransport
    from test_goal_program_api import setup_app, headers
    from test_goal_programs import preview

    monkeypatch.setattr("app.goal_adjustments._local_date", lambda _: "2026-09-01")
    runtime, app, client, version = setup_app(tmp_path)
    goals = runtime.goal_programs
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate")
    _, _, _, _, gateway = _plane(tmp_path, monkeypatch, execute=None, database=runtime.db)
    goals.compiler = runtime.goal_adjustments.compiler = GoalProgramCompiler(gateway)
    candidate = active["structure"]
    candidate["actions"][1]["title"] = "Adjusted training"
    recorder = CommittedSnapshotTransport(runtime.db, "local-user", "CS-GP-02", [
        _answer("not JSON"), _answer(json.dumps(candidate)),
    ])
    monkeypatch.setattr(gateway, "_execute_attempt", recorder)
    response = client.post(
        f"/api/programs/{active['id']}/adjustments",
        json={"reason": "Change training", "expected_version": active["version"]},
        headers=headers(app, "adjust"),
    )
    assert response.status_code == 200, response.text
    assert len(recorder.observations) == 2
    first, repair = [ModelInputSnapshotStore(runtime.db).load("local-user", item["snapshot_id"])
                     for item in recorder.observations]
    assert first.id != repair.id and first.content_json != repair.content_json
    assert len(repair.to_request().messages) == len(first.to_request().messages) + 2
    assert [row["purpose"] for row in _invocations(runtime.db)] == ["adjust_goal_program"] * 2
