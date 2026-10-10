"""Native goal calls use the production gateway and committed PG identities."""
import json
import pytest


@pytest.mark.asyncio
async def test_goal_native_gateway_snapshot_and_trace_survive_approval(isolated_postgres_test, migrated_postgres_url, tmp_path, monkeypatch):
    from app.db import Database
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.memory import MemoryService
    from app.runtime import AgentRuntime, MockModelGateway
    from app.tools import create_default_registry
    from app.model_control import ModelControlStore, RoutedModelGateway
    from app.model_gateway import ModelResponse, UsageBuckets, Timing
    from test_snapshot_gateway import _configured_control_plane
    from test_conversation_loop import tool
    monkeypatch.setenv('BETTER_AGENT_LOOP_MODE', 'loop')
    monkeypatch.setenv('BETTER_AGENT_COST_MODE', 'observe')
    from app.agent_loop import AgentLoop, Failed
    original_run = AgentLoop.run
    async def checked_run(self, *args, **kwargs):
        result = await original_run(self, *args, **kwargs)
        if isinstance(result, Failed):
            raise result.error
        return result
    monkeypatch.setattr(AgentLoop, 'run', checked_run)
    db = Database(migrated_postgres_url, workspace=tmp_path / 'workspace')
    try:
        _, bundle, _ = _configured_control_plane(tmp_path, monkeypatch, database=db)
        events, approvals = EventStore(db), ApprovalService(db)
        model = MockModelGateway(plan_steps=[{'id': 's', 'title': 'write'}])
        runtime = AgentRuntime(db=db, events=events, plans=PlanVersionService(db), approvals=approvals,
            checkpoints=CheckpointStore(db), memory=MemoryService(db, events, tmp_path / 'memory'),
            tools=create_default_registry(tmp_path / 'workspace', db=db, approval_service=approvals), model=model)
        from types import SimpleNamespace
        runtime.evolution = SimpleNamespace(assign_run=lambda *args, **kwargs: (bundle.id, None))
        run = await runtime.create_goal('goal', 'write a note')
        runtime.evolution = None
        await runtime.handle_message(run.id, 'start')
        seen = []
        async def attempt(profile, request, **kwargs):
            with db.connection() as connection:
                row = connection.execute('SELECT context_snapshot_id,execution_context_json FROM model_invocations WHERE run_id=? ORDER BY created_at DESC LIMIT 1', (run.id,)).fetchone()
            assert row['context_snapshot_id'] and row['execution_context_json']
            seen.append(json.loads(row['execution_context_json']))
            calls = [tool('write_note', {'path': 'once.md', 'content': 'once'})] if len(seen) == 1 else [tool('finish_step', {'output': 'saved'})]
            return ModelResponse('', calls, 'tool_calls', UsageBuckets(1, 0, 0, 1, 0), Timing(0, 0, 1), 1)
        model.gateway = RoutedModelGateway(db, ModelControlStore(db), execute_attempt=attempt)
        await runtime.approve_plan(run.id, 1)
        assert not (tmp_path / 'workspace/once.md').exists()
        assert runtime.pending_approvals(run.id), json.dumps([{'type': e.type, 'data': e.data} for e in events.list(run.id)], default=str)
        approval = runtime.pending_approvals(run.id)[0]
        await runtime.grant_approval(approval.id)
        assert runtime.get_run(run.id).state == 'COMPLETED'
        assert (tmp_path / 'workspace/once.md').read_text() == 'once'
        assert len(seen) == 2
        from app.execution_context import deserialize_context
        contexts = [deserialize_context(value) for value in seen]
        assert contexts[0].trace_id == contexts[1].trace_id
        assert contexts[0].span_id != contexts[1].span_id
        assert all(c.owner_id == 'local-user' and c.run_id == run.id and c.runtime_bundle_id == bundle.id for c in contexts)
        from app.event_envelope import EventMetadata
        traced = [(e, EventMetadata.from_dict(json.loads(e.envelope_json)))
                  for e in events.list(run.id) if e.envelope_json]
        proposed = [(e, m) for e, m in traced if e.type == 'tool.proposed' and e.data['name'] == 'write_note']
        assert [m.attempt for _, m in proposed] == [1, 2]
        assert proposed[0][1].context.span_id != proposed[1][1].context.span_id
        completed = next(m for e, m in traced if e.type == 'tool.execution.finished')
        assert completed.context.span_id == proposed[1][1].context.span_id
        assert completed.outcome.effect.value == 'applied'
        for event, metadata in traced:
            if event.type == 'model.attempt.finished':
                start = next(m for e, m in traced if e.type == 'model.attempt.started'
                             and m.operation_id == metadata.operation_id and m.attempt == metadata.attempt)
                assert metadata.context.span_id == start.context.span_id
    finally:
        db.close()
