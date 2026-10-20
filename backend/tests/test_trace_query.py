import json

import pytest
from fastapi.testclient import TestClient

from app.conversation import ConversationService
from app.db import Database
from app.main import create_app
from app.runtime import MockModelGateway
from app.trace_query import LEGACY_SCAN_LIMIT, SOURCE_SEEK_BUDGET, TraceNotFound, TraceQuery


@pytest.fixture()
def database(migrated_postgres_url, isolated_postgres_test, tmp_path, monkeypatch):
    """PostgreSQL-backed database with a per-test empty schema."""
    monkeypatch.delenv("BETTER_AGENT_TEST_ALLOW_SQLITE", raising=False)
    monkeypatch.setenv("DATABASE_URL", str(migrated_postgres_url))
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    assert db.backend == "postgresql"
    yield db
    db.close()


def _make_runtime(database, tmp_path, model):
    from app.domain import ApprovalService, CheckpointStore, PlanVersionService
    from app.events import EventStore
    from app.memory import MemoryService
    from app.runtime import AgentRuntime
    from app.tools import create_default_registry

    events = EventStore(database)
    approvals = ApprovalService(database)
    return AgentRuntime(
        db=database,
        events=events,
        plans=PlanVersionService(database),
        approvals=approvals,
        checkpoints=CheckpointStore(database),
        memory=MemoryService(database, events, tmp_path / "memory"),
        tools=create_default_registry(tmp_path / "workspace", db=database, approval_service=approvals),
        model=model,
    )


def _seed(database, owner="local-user"):
    service = ConversationService(database)
    thread = service.create_thread("trace", owner_id=owner)
    turn = service.accept_turn(thread.id, "one", "hello", [], owner_id=owner)
    return service, thread, turn


def test_by_thread_and_by_trace_merge_one_conversation(database):
    service, thread, turn = _seed(database)
    query = TraceQuery(service.db)
    thread_view = query.by_thread(thread.id, owner_id="local-user")
    assert thread_view["count"] >= 1
    traces = {event.get("context", {}).get("context", {}).get("trace_id") for event in thread_view["events"]}
    traces.discard(None)
    assert len(traces) == 1
    trace_id = traces.pop()

    trace_view = query.by_trace(trace_id, owner_id="local-user")
    assert trace_view["scope"]["trace_id"] == trace_id
    assert {event["event_id"] for event in trace_view["events"]} == {
        event["event_id"] for event in thread_view["events"]
    }
    # The turn's model/tool/loop association is visible on the merged stream.
    kinds = {event.get("stream_kind") for event in trace_view["events"]}
    assert kinds <= {"thread", "run"} and kinds
    # Default projection withholds execution-internal references.
    assert all("snapshot_refs" not in event for event in trace_view["events"])


def test_trace_is_owner_isolated_and_legacy_events_are_marked(database):
    service, thread, turn = _seed(database)
    query = TraceQuery(service.db)
    with pytest.raises(TraceNotFound):
        query.by_thread(thread.id, owner_id="someone-else")
    with pytest.raises(TraceNotFound):
        query.by_trace("0" * 32, owner_id="local-user")

    # A row written before envelopes existed must never disappear silently.
    with service.db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
            "VALUES (1,?,?,?,?,'legacy.note','2020-01-01T00:00:00+00:00','runtime','{}',NULL)",
            ("tevt_legacy", seq, thread.id, turn.turn_id),
        )
    view = query.by_thread(thread.id, owner_id="local-user")
    legacy = [event for event in view["events"] if event["type"] == "legacy.note"]
    assert legacy and legacy[0]["compatibility"] == "legacy_context_missing"


def _finish_turn(service, thread, turn):
    with service.db.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id=?", (turn.turn_id,))
        connection.execute("UPDATE threads SET active_turn_id=NULL WHERE id=?", (thread.id,))


def _seed_research(database):
    service = ConversationService(database)
    thread = service.create_thread("research")
    first = service.accept_turn(thread.id, "one", "hello", [])
    _finish_turn(service, thread, first)
    second = service.accept_turn(thread.id, "two", "again", [])
    now = "2026-01-01T00:00:00+00:00"
    with service.db.transaction() as connection:
        for job_id, turn, occurrence in (("job-a", first, "occ-a"), ("job-b", second, "occ-b")):
            connection.execute(
                "INSERT INTO research_jobs(id,thread_id,source_turn_id,trigger_kind,occurrence_key,topic,"
                "source_scopes_json,status,phase,available_at,created_at,updated_at) "
                "VALUES (?,?,?,'manual',?,'topic','[\"web\"]','COMPLETED','completed',?,?,?)",
                (job_id, thread.id, turn.turn_id, occurrence, now, now, now),
            )
    _finish_turn(service, thread, second)
    service.events.append(thread.id, first.turn_id, "research.started", "research_worker", {"job_id": "job-a", "attempt": 1})
    service.events.append(thread.id, first.turn_id, "research.plan_ready", "research_worker", {"job_id": "job-a"})
    service.events.append(thread.id, second.turn_id, "research.started", "research_worker", {"job_id": "job-b", "attempt": 1})
    return service, thread, first, second


def test_deleted_thread_hides_thread_and_trace(database):
    service, thread, turn = _seed(database)
    query = TraceQuery(service.db)
    trace_id = next(
        event["context"]["context"]["trace_id"]
        for event in query.by_thread(thread.id, owner_id="local-user")["events"]
    )
    _finish_turn(service, thread, turn)
    service.delete_thread(thread.id)
    with pytest.raises(TraceNotFound):
        query.by_thread(thread.id, owner_id="local-user")
    with pytest.raises(TraceNotFound):
        query.by_trace(trace_id, owner_id="local-user")


def test_deleted_thread_hides_its_task(database):
    service, thread, _first, _second = _seed_research(database)
    query = TraceQuery(service.db)
    assert query.by_task("research", "job-a", owner_id="local-user")["events"]
    service.delete_thread(thread.id)
    with pytest.raises(TraceNotFound):
        query.by_task("research", "job-a", owner_id="local-user")


def test_task_query_is_scoped_to_one_task(database):
    service, thread, _first, _second = _seed_research(database)
    query = TraceQuery(service.db)
    first = query.by_task("research", "job-a", owner_id="local-user")
    second = query.by_task("research", "job-b", owner_id="local-user")
    assert first["task_scope"] == "exact" and second["task_scope"] == "exact"
    assert first["events"] and all(event["payload"].get("job_id") == "job-a" for event in first["events"])
    assert second["events"] and all(event["payload"].get("job_id") == "job-b" for event in second["events"])
    ids_first = {event["event_id"] for event in first["events"]}
    ids_second = {event["event_id"] for event in second["events"]}
    assert ids_first and ids_second and ids_first.isdisjoint(ids_second)


def test_task_query_filters_before_pagination(database):
    service, thread, first, _second = _seed_research(database)
    for index in range(250):
        service.events.append(thread.id, first.turn_id, f"unrelated.{index}", "runtime", {"index": index})
    service.events.append(thread.id, first.turn_id, "research.target", "research_worker", {"job_id": "job-a"})
    view = TraceQuery(service.db).by_task("research", "job-a", owner_id="local-user", limit=1)
    assert view["events"]
    assert view["events"][0]["payload"].get("job_id") == "job-a"


def test_expert_task_query_and_deleted_thread(database):
    from app.agents import AgentTaskService
    from app.behavior import BehaviorBundleService
    db = database
    bundle = BehaviorBundleService(db).ensure({"code": "test"})
    BehaviorBundleService(db).activate("stable", bundle.id, "one")
    conversation = ConversationService(db)
    thread = conversation.create_thread("expert")
    source = conversation.accept_turn(thread.id, "one", "expert", [])
    tasks = AgentTaskService(db, thread_events=conversation.events)
    run = tasks.create_run("local-user", "topic", {}, bundle.id, thread_id=thread.id,
                           parent_turn_id=source.turn_id, idempotency_key="one", append_thread_message=False)
    query = TraceQuery(db)
    view = query.by_task("expert", run["id"], owner_id="local-user")
    assert view["events"]
    assert all(event["payload"].get("agent_run_id") == run["id"] or event["payload"].get("run_id") == run["id"]
               for event in view["events"])
    # A subtask URL carries agent_tasks.id, while run-level events carry
    # agent_run_id.  The task query must retain task-scoped events without
    # widening them to the whole run.
    task_id = run["coordinator_task_id"]
    conversation.events.append(thread.id, source.turn_id, "expert.task.note", "worker",
                                {"task_id": task_id, "agent_run_id": run["id"]})
    task_view = query.by_task("expert", task_id, owner_id="local-user")
    assert task_view["events"]
    assert all(event["payload"].get("task_id") == task_id for event in task_view["events"])
    _finish_turn(conversation, thread, source)
    conversation.delete_thread(thread.id)
    with pytest.raises(TraceNotFound):
        query.by_task("expert", run["id"], owner_id="local-user")


def test_trace_query_paginates_with_a_stable_cursor(database):
    service, thread, turn = _seed(database)
    for index in range(5):
        service.events.append(thread.id, turn.turn_id, f"step.{index}", "runtime", {"index": index})
    query = TraceQuery(service.db)
    first = query.by_thread(thread.id, owner_id="local-user", limit=2)
    assert first["count"] == 2 and isinstance(first["next_cursor"], str)
    second = query.by_thread(thread.id, owner_id="local-user", limit=2, cursor=first["next_cursor"])
    assert second["count"] == 2
    ids_first = [event["event_id"] for event in first["events"]]
    ids_second = [event["event_id"] for event in second["events"]]
    assert set(ids_first).isdisjoint(ids_second)
    # Re-reading the same page is byte-for-byte stable.
    assert [event["event_id"] for event in query.by_thread(thread.id, owner_id="local-user", limit=2)["events"]] == ids_first


def test_legacy_trace_paginates_after_the_first_page(database):
    service, thread, turn = _seed(database)
    query = TraceQuery(service.db)
    trace_id = query.by_thread(thread.id, owner_id="local-user")["events"][0]["context"]["context"]["trace_id"]
    with service.db.transaction() as connection:
        row = connection.execute(
            "SELECT envelope_json FROM thread_events WHERE thread_id=? ORDER BY seq LIMIT 1", (thread.id,)
        ).fetchone()
        next_seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM thread_events WHERE thread_id=?", (thread.id,)
        ).fetchone()[0]
        for index in range(4):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
                "VALUES (1,?,?,?,?,?,clock_timestamp()::text,?,'{}',?)",
                (f"legacy_page_{index}", next_seq + index, thread.id, turn.turn_id,
                 f"legacy.{index}", "runtime", row["envelope_json"]),
            )
    first = query.by_trace(trace_id, owner_id="local-user", limit=2)
    second = query.by_trace(trace_id, owner_id="local-user", limit=2, cursor=first["next_cursor"])
    assert first["count"] == 2
    assert second["count"] >= 1
    assert set(event["event_id"] for event in first["events"]).isdisjoint(
        event["event_id"] for event in second["events"]
    )


def test_legacy_trace_filters_before_page_cap(database):
    service, thread, turn = _seed(database)
    query = TraceQuery(service.db)
    with service.db.connection() as connection:
        envelope = connection.execute(
            "SELECT envelope_json FROM thread_events WHERE thread_id=? ORDER BY seq LIMIT 1",
            (thread.id,),
        ).fetchone()["envelope_json"]
        target_envelope = json.loads(envelope)
        target_trace = "d" * 32
        target_envelope["context"]["context"]["trace_id"] = target_trace
        target_envelope = json.dumps(target_envelope, ensure_ascii=False)
    with service.db.transaction() as connection:
        next_seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM thread_events WHERE thread_id=?", (thread.id,)
        ).fetchone()[0]
        # These unrelated legacy rows fill the first page-sized SQL cap.
        for index in range(8):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
                "VALUES (1,?,?,?,?,?,clock_timestamp()::text,?,'{}',NULL)",
                (f"legacy_unrelated_{index}", next_seq + index, thread.id, turn.turn_id,
                 f"legacy.unrelated.{index}", "runtime"),
            )
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
            "VALUES (1,?,?,?,?,?,clock_timestamp()::text,?,'{}',?)",
            ("legacy_target", next_seq + 8, thread.id, turn.turn_id,
             "legacy.target", "runtime", target_envelope),
        )

    view = query.by_trace(target_trace, owner_id="local-user", limit=1)
    assert view["count"] == 1
    assert view["events"][0]["event_id"] == "legacy_target"


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 10 ** 9}, {"limit": "many"}, {"cursor": -1}])
def test_trace_query_rejects_an_invalid_page(database, kwargs):
    service, thread, _ = _seed(database)
    with pytest.raises(ValueError):
        TraceQuery(service.db).by_thread(thread.id, owner_id="local-user", **kwargs)


def test_trace_lookup_uses_the_indexed_column(database):
    service, thread, _ = _seed(database)
    query = TraceQuery(service.db)
    assert query._trace_column_present() is True
    with service.db.connection() as connection:
        rows = connection.execute("SELECT trace_id FROM thread_events WHERE thread_id=?", (thread.id,)).fetchall()
    assert rows and all(row["trace_id"] for row in rows)
    trace_id = query.by_thread(thread.id, owner_id="local-user")["events"][0]["context"]["context"]["trace_id"]
    assert query.by_trace(trace_id, owner_id="local-user")["events"]


def _insert_legacy_event(service, thread, turn_id, trace_id, index):
    from dataclasses import replace
    from app.event_envelope import EventMetadata
    from app.execution_context import create_root_context
    context = replace(
        create_root_context(owner_id="local-user", thread_id=thread.id, turn_id=turn_id,
                            run_id=f"chat-turn:{turn_id}"),
        trace_id=trace_id,
    )
    envelope = json.dumps(EventMetadata(context, "runtime", turn_id).to_dict())
    with service.db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,"
            "data_json,envelope_json,trace_id) VALUES (1,?,?,?,?,'legacy.step',?,'runtime','{}',?,NULL)",
            (f"tevt-legacy-{index}", seq, thread.id, turn_id, f"2020-01-01T00:00:{index:02d}+00:00", envelope),
        )


@pytest.mark.asyncio
async def test_direct_goal_run_trace_is_owner_readable(database, tmp_path, monkeypatch):
    """A run with no source turn is still owner-readable by its trace.

    Direct goal runs persist their identity in ``agent_loop_context`` and write
    envelope events; the trace query must authorize them through that context
    instead of dropping every run whose ``source_turn_id`` is NULL.
    """
    import json as _json
    from app.event_envelope import EventMetadata
    from app.runtime import MockModelGateway, ModelDecision
    monkeypatch.setenv("BETTER_AGENT_LOOP_MODE", "loop")
    runtime = _make_runtime(database, tmp_path, MockModelGateway(
        plan_steps=[{"id": "s", "title": "step"}], decisions=[ModelDecision.complete("done")]))
    run = await runtime.create_goal("goal", "goal")
    await runtime.handle_message(run.id, "start")
    await runtime.approve_plan(run.id, 1)
    events = [event for event in runtime.events.list(run.id) if event.envelope_json]
    assert events
    trace_id = EventMetadata.from_dict(_json.loads(events[0].envelope_json)).trace_id
    query = TraceQuery(runtime.db)
    view = query.by_trace(trace_id, owner_id="local-user")
    assert view["events"] and any(event["stream_kind"] == "run" for event in view["events"])
    with pytest.raises(TraceNotFound):
        query.by_trace(trace_id, owner_id="someone-else")


def test_legacy_scan_truncation_is_reported(database, monkeypatch):
    service, thread, turn = _seed(database)
    trace_id = "b" * 32
    for index in range(3):
        _insert_legacy_event(service, thread, turn.turn_id, trace_id, index)
    monkeypatch.setattr("app.trace_query.LEGACY_SCAN_LIMIT", 2)
    view = TraceQuery(service.db).by_trace(trace_id, owner_id="local-user", limit=1)
    # The fallback hit its cap; the result must say so instead of implying it is complete.
    assert view.get("legacy_scan_truncated") is True


def test_trace_api_authorizes_before_reading(database, tmp_path, monkeypatch):
    runtime = _make_runtime(database, tmp_path, MockModelGateway())
    client = TestClient(create_app(runtime=runtime))
    thread = runtime.conversation.create_thread("local")
    runtime.conversation.accept_turn(thread.id, "one", "hello", [])
    headers = {"host": "127.0.0.1:8000"}

    response = client.get(f"/api/threads/{thread.id}/trace", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    trace_id = body["events"][0]["context"]["context"]["trace_id"]

    by_trace = client.get(f"/api/traces/{trace_id}", headers=headers)
    assert by_trace.status_code == 200 and by_trace.json()["scope"]["trace_id"] == trace_id

    foreign = {**headers, "x-owner-id": "other"}
    hidden = client.get(f"/api/threads/{thread.id}/trace", headers=foreign)
    assert hidden.status_code == 404
    assert "local-user" not in hidden.text
    assert client.get(f"/api/traces/{trace_id}", headers=foreign).status_code == 404
    assert client.get("/api/tasks/research/missing/trace", headers=foreign).status_code == 404
    assert client.get(f"/api/tasks/nope/missing/trace", headers=headers).status_code == 400
    assert client.get(f"/api/threads/{thread.id}/trace?limit=0", headers=headers).status_code == 400
    audited = client.get(f"/api/threads/{thread.id}/trace?audit=true", headers=headers)
    assert audited.status_code == 200
    assert "snapshot_refs" not in audited.text
    assert audited.json()["sources"]["deferred"] == []
    assert "notification" in audited.json()["sources"]["connected"]


def test_http_trace_redacts_nested_secrets_and_malformed_envelopes(database, tmp_path):
    runtime = _make_runtime(database, tmp_path, MockModelGateway())
    client = TestClient(create_app(runtime=runtime))
    thread = runtime.conversation.create_thread("local")
    turn = runtime.conversation.accept_turn(thread.id, "one", "hello", [])
    runtime.conversation.events.append(thread.id, turn.turn_id, "note.saved", "runtime", {
        "wrapper": {"token": "sekrit-token", "content": "private prompt text", "job_id": "job-a"},
    })
    with runtime.db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
            "VALUES (1,'tevt-bad',?,?,?,'note.saved','2026-01-01T00:00:00+00:00','runtime','{}',?)",
            (seq, thread.id, turn.turn_id, '{"schema_version":99,"secret":"super-secret-value"}'),
        )
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
            "VALUES (1,'tevt-broken',?,?,?,'note.saved','2026-01-01T00:00:01+00:00','runtime','{}','{')",
            (seq + 1, thread.id, turn.turn_id),
        )
    headers = {"host": "127.0.0.1:8000"}
    response = client.get(f"/api/threads/{thread.id}/trace?audit=true&limit=1000", headers=headers)
    assert response.status_code == 200
    text = response.text
    assert "sekrit-token" not in text
    assert "private prompt text" not in text
    assert "super-secret-value" not in text
    body = response.json()
    by_id = {event["event_id"]: event for event in body["events"]}
    assert by_id["tevt-bad"]["compatibility"] == "unsupported_version"
    assert by_id["tevt-broken"]["compatibility"] == "malformed_envelope"
    saved = next(event for event in body["events"] if event["type"] == "note.saved" and event.get("payload"))
    assert saved["payload"]["wrapper"]["content"] == {"length": len("private prompt text"), "redacted": True}
    assert "token" not in saved["payload"]["wrapper"]


def test_same_timestamp_and_later_insert_keep_pages_stable(database):
    service, thread, turn = _seed(database)
    query = TraceQuery(service.db)
    with service.db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
        for offset, name in ((1, "tevt-same-a"), (2, "tevt-same-b")):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json) "
                "VALUES (1,?,?,?,?,'same.step','2024-01-01T00:00:00+00:00','runtime','{}')",
                (name, seq + offset, thread.id, turn.turn_id),
            )
        connection.execute(
            "UPDATE threads SET next_event_seq=? WHERE id=?", (seq + 3, thread.id),
        )
    with pytest.raises(ValueError):
        query.by_thread(thread.id, owner_id="local-user", limit=1, cursor=10_000)
    page = query.by_thread(thread.id, owner_id="local-user", limit=1)
    # Walk until the same-timestamp pair and check seq order, then insert after it.
    seen = []
    cursor = None
    while True:
        view = query.by_thread(thread.id, owner_id="local-user", limit=1, cursor=cursor)
        if not view["events"]:
            break
        seen.append(view["events"][0]["event_id"])
        if view["next_cursor"] is None:
            break
        cursor = view["next_cursor"]
    assert seen.index("tevt-same-a") < seen.index("tevt-same-b")
    service.events.append(thread.id, turn.turn_id, "same.later", "runtime", {"index": 1})
    replay = [event["event_id"] for event in query.by_thread(thread.id, owner_id="local-user", limit=1)["events"]]
    assert replay == [seen[0]]
    again = []
    cursor = None
    while True:
        view = query.by_thread(thread.id, owner_id="local-user", limit=1, cursor=cursor)
        if not view["events"]:
            break
        again.append(view["events"][0]["event_id"])
        if view["next_cursor"] is None:
            break
        cursor = view["next_cursor"]
    assert len(again) == len(set(again))
    assert set(seen) <= set(again)
    assert len(again) == len(seen) + 1


def test_duplicate_event_id_across_streams_is_returned_once(database):
    from app.agents import AgentTaskService
    from app.behavior import BehaviorBundleService
    db = database
    bundle = BehaviorBundleService(db).ensure({"code": "test"})
    BehaviorBundleService(db).activate("stable", bundle.id, "one")
    conversation = ConversationService(db)
    thread = conversation.create_thread("expert")
    source = conversation.accept_turn(thread.id, "one", "expert", [])
    tasks = AgentTaskService(db, thread_events=conversation.events)
    run = tasks.create_run("local-user", "topic", {}, bundle.id, thread_id=thread.id,
                           parent_turn_id=source.turn_id, idempotency_key="one", append_thread_message=False)
    with db.transaction() as connection:
        copied = connection.execute(
            "SELECT event_id FROM thread_events WHERE thread_id=? AND type='expert.run.queued'", (thread.id,),
        ).fetchone()["event_id"]
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM agent_events WHERE agent_run_id=?", (run["id"],),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO agent_events(event_id,agent_run_id,seq,task_id,type,actor,data_json,occurred_at) "
            "VALUES (?,?,?,?,'agent.run.created','coordinator','{}','2026-01-01T00:00:00+00:00')",
            (copied, run["id"], seq, run["coordinator_task_id"]),
        )
    view = TraceQuery(db).by_task("expert", run["id"], owner_id="local-user")
    assert [event["event_id"] for event in view["events"]].count(copied) == 1
    assert "execution_context" not in json.dumps(view)


def _walk(query, thread_id, *, limit=1):
    """Read every page. The cursor is whatever the previous page returned."""
    found = []
    cursor = None
    pages = 0
    while pages < 50:
        view = query.by_thread(
            thread_id, owner_id="local-user", limit=limit,
            **({} if cursor is None else {"cursor": cursor}),
        )
        ids = [event["event_id"] for event in view["events"]]
        assert len(ids) == len(set(ids))
        assert set(ids).isdisjoint(found)
        found.extend(ids)
        pages += 1
        # One page reads a fixed window, not a window that grows with the cursor.
        assert query._last_thread_params[-1] == limit + 1
        if view["next_cursor"] is None:
            return found, view
        cursor = view["next_cursor"]
    raise AssertionError("pagination did not terminate")


def test_mixed_history_pages_keep_occurred_at_and_identity(database):
    """Legacy, malformed and unsupported rows keep the stored time and id."""
    service, thread, turn = _seed(database)
    query = TraceQuery(service.db)
    with service.db.connection() as connection:
        envelope = connection.execute(
            "SELECT envelope_json FROM thread_events WHERE thread_id=? AND envelope_json IS NOT NULL LIMIT 1",
            (thread.id,),
        ).fetchone()["envelope_json"]
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
    stamped = (
        ("hist-legacy", "2020-01-01T00:00:00+00:00", "legacy.note", None),
        ("hist-valid", "2020-01-02T00:00:00+00:00", "note.saved", envelope),
        ("hist-malformed", "2020-01-03T00:00:00+00:00", "note.saved", "{"),
        ("hist-unsupported", "2020-01-04T00:00:00+00:00", "note.saved", '{"schema_version":99}'),
    )
    with service.db.transaction() as connection:
        for offset, (event_id, occurred_at, event_type, body) in enumerate(stamped, start=1):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
                "VALUES (1,?,?,?,?,?,?,?,'{}',?)",
                (event_id, seq + offset, thread.id, turn.turn_id, event_type, occurred_at, "runtime", body),
            )
        connection.execute("UPDATE threads SET next_event_seq=? WHERE id=?", (seq + 5, thread.id))
    found, _last = _walk(query, thread.id, limit=2)
    assert found[:4] == ["hist-legacy", "hist-valid", "hist-malformed", "hist-unsupported"]
    whole = query.by_thread(thread.id, owner_id="local-user", limit=1000)
    by_id = {event["event_id"]: event for event in whole["events"]}
    for event_id, occurred_at, _event_type, _body in stamped:
        assert by_id[event_id]["occurred_at"] == occurred_at
        assert by_id[event_id]["event_id"] == event_id
    assert [event["event_id"] for event in whole["events"]] == found
    assert by_id["hist-legacy"]["compatibility"] == "legacy_context_missing"
    assert by_id["hist-malformed"]["compatibility"] == "malformed_envelope"
    assert by_id["hist-unsupported"]["compatibility"] == "unsupported_version"


def test_same_timestamp_across_streams_uses_one_sort_key(database):
    from app.agents import AgentTaskService
    from app.behavior import BehaviorBundleService

    db = database
    bundle = BehaviorBundleService(db).ensure({"code": "test"})
    BehaviorBundleService(db).activate("stable", bundle.id, "one")
    service = ConversationService(db)
    thread = service.create_thread("expert")
    turn = service.accept_turn(thread.id, "one", "expert", [])
    tasks = AgentTaskService(db, thread_events=service.events)
    run = tasks.create_run(
        "local-user", "topic", {}, bundle.id, thread_id=thread.id,
        parent_turn_id=turn.turn_id, idempotency_key="one", append_thread_message=False,
    )
    with db.connection() as connection:
        agent = connection.execute(
            "SELECT event_id,occurred_at FROM agent_events WHERE agent_run_id=? ORDER BY seq LIMIT 1",
            (run["id"],),
        ).fetchone()
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json) "
            "VALUES (1,'cross-thread',?,?,?,'same.step',?,'runtime','{}')",
            (seq, thread.id, turn.turn_id, agent["occurred_at"]),
        )
        connection.execute("UPDATE threads SET next_event_seq=? WHERE id=?", (seq + 1, thread.id))
    query = TraceQuery(db)
    found, _last = _walk(query, thread.id, limit=1)
    whole = {
        event["event_id"]: event
        for event in query.by_thread(thread.id, owner_id="local-user", limit=1000)["events"]
    }
    pair = [whole[agent["event_id"]], whole["cross-thread"]]
    ordered = sorted(pair, key=lambda event: (
        event["occurred_at"], event["stream_kind"], event["stream_id"], event["seq"], event["event_id"],
    ))
    assert pair[0]["occurred_at"] == pair[1]["occurred_at"]
    assert {event["stream_kind"] for event in pair} == {"agent", "thread"}
    positions = [found.index(event["event_id"]) for event in ordered]
    assert positions == sorted(positions)
    assert found.count(agent["event_id"]) == 1 and found.count("cross-thread") == 1


def test_insert_ahead_of_the_cursor_does_not_shift_the_snapshot(database):
    """Rows committed after the first page stay out of that walk.

    A new row may sort before the cursor. The open snapshot still returns each
    row that existed at the first page once, and a new walk includes the insert.
    """
    service, thread, turn = _seed(database)
    for index in range(3):
        service.events.append(thread.id, turn.turn_id, f"step.{index}", "runtime", {"index": index})
    query = TraceQuery(service.db)
    before = [
        event["event_id"]
        for event in query.by_thread(thread.id, owner_id="local-user", limit=1000)["events"]
    ]
    first = query.by_thread(thread.id, owner_id="local-user", limit=1)
    assert first["events"] and first["next_cursor"] is not None
    with service.db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json) "
            "VALUES (1,'inserted-ahead',?,?,?,'note.ahead','2001-01-01T00:00:00+00:00','runtime','{}')",
            (seq, thread.id, turn.turn_id),
        )
        connection.execute("UPDATE threads SET next_event_seq=? WHERE id=?", (seq + 1, thread.id))
    rest = []
    cursor = first["next_cursor"]
    while cursor is not None:
        page = query.by_thread(thread.id, owner_id="local-user", limit=1, cursor=cursor)
        rest.extend(event["event_id"] for event in page["events"])
        cursor = page["next_cursor"]
    walked = [first["events"][0]["event_id"], *rest]
    assert walked == before
    assert len(walked) == len(set(walked))
    fresh, _last = _walk(query, thread.id, limit=1)
    assert fresh == ["inserted-ahead", *before]


def test_http_trace_keeps_bad_envelopes_on_the_page(database, tmp_path):
    runtime = _make_runtime(database, tmp_path, MockModelGateway())
    client = TestClient(create_app(runtime=runtime))
    thread = runtime.conversation.create_thread("local")
    turn = runtime.conversation.accept_turn(thread.id, "one", "hello", [])
    headers = {"host": "127.0.0.1:8000"}
    with runtime.db.connection() as connection:
        envelope = json.loads(connection.execute(
            "SELECT envelope_json FROM thread_events WHERE thread_id=? AND envelope_json IS NOT NULL LIMIT 1",
            (thread.id,),
        ).fetchone()["envelope_json"])
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
        trace_id = envelope["context"]["context"]["trace_id"]
    missing = dict(envelope)
    missing.pop("context", None)
    missing.pop("source", None)
    nested = json.loads(json.dumps(envelope))
    nested["context"] = ["not-a-context"]
    invalid = json.loads(json.dumps(envelope))
    invalid["attempt"] = 0
    invalid["phase"] = "not-a-phase"
    foreign = json.loads(json.dumps(envelope))
    foreign["context"]["context"]["owner_id"] = "other-owner"
    rows = (
        ("bad-array", "2020-02-01T00:00:00+00:00", json.dumps(["not-an-envelope", trace_id])),
        ("bad-null", "2020-02-02T00:00:00+00:00", "null"),
        ("bad-missing", "2020-02-03T00:00:00+00:00", json.dumps(missing)),
        ("bad-nested", "2020-02-04T00:00:00+00:00", json.dumps(nested)),
        ("bad-invalid", "2020-02-05T00:00:00+00:00", json.dumps(invalid)),
        ("bad-owner", "2020-02-06T00:00:00+00:00", json.dumps(foreign)),
    )
    with runtime.db.transaction() as connection:
        for offset, (event_id, occurred_at, body) in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
                "VALUES (1,?,?,?,?,?,?,?,'{}',?)",
                (event_id, seq + offset, thread.id, turn.turn_id, "note.bad", occurred_at, "runtime", body),
            )
        # A legacy row whose body is an array still has to be skipped safely.
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json,trace_id) "
            "VALUES (1,'bad-legacy-array',?,?,?,'note.bad','2020-02-07T00:00:00+00:00','runtime','{}',?,NULL)",
            (seq + 7, thread.id, turn.turn_id, json.dumps([{"trace_id": trace_id}])),
        )
    listed = client.get(f"/api/threads/{thread.id}/trace?limit=1000", headers=headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    by_id = {event["event_id"]: event for event in body["events"]}
    assert "bad-owner" not in by_id
    assert "other-owner" not in listed.text
    for event_id, occurred_at, _body in rows:
        if event_id == "bad-owner":
            continue
        assert by_id[event_id]["compatibility"] in {"malformed_envelope", "unsupported_version"}
        assert by_id[event_id]["occurred_at"] == occurred_at
        assert by_id[event_id]["event_id"] == event_id
    assert any(event.get("context", {}).get("context", {}).get("trace_id") == trace_id for event in body["events"])
    traced = client.get(f"/api/traces/{trace_id}?limit=1000", headers=headers)
    assert traced.status_code == 200, traced.text
    assert any(event.get("context", {}).get("context", {}).get("trace_id") == trace_id for event in traced.json()["events"])
    assert "other-owner" not in traced.text


def test_expert_subtask_reads_its_own_agent_events(database):
    from app.agents import AgentTaskService
    from app.behavior import BehaviorBundleService

    db = database
    bundle = BehaviorBundleService(db).ensure({"code": "test"})
    BehaviorBundleService(db).activate("stable", bundle.id, "one")
    service = ConversationService(db)
    thread = service.create_thread("expert")
    turn = service.accept_turn(thread.id, "one", "expert", [])
    tasks = AgentTaskService(db, thread_events=service.events)
    run = tasks.create_run(
        "local-user", "topic", {}, bundle.id, thread_id=thread.id,
        parent_turn_id=turn.turn_id, idempotency_key="one", append_thread_message=False,
    )
    parent = tasks.claim_next("coordinator", 30)
    children = tasks.fan_out(parent["id"], "coordinator", parent["lease_epoch"], [
        {"child_key": "a", "role": "researcher", "objective": "a"},
        {"child_key": "b", "role": "critic", "objective": "b"},
    ], "ALL_DONE")
    claimed = [tasks.claim_next("worker", 30), tasks.claim_next("worker", 30)]
    assert {item["id"] for item in claimed} == {item["id"] for item in children}
    query = TraceQuery(db)
    views = {
        child["id"]: query.by_task("expert", child["id"], owner_id="local-user")
        for child in children
    }
    for child in children:
        types = {event["type"] for event in views[child["id"]]["events"]}
        assert {"agent.task.created", "agent.task.started"} <= types
        assert all(event["payload"].get("task_id") == child["id"] for event in views[child["id"]]["events"])
        assert all(
            event.get("compatibility") == "legacy_context_missing" and "context" not in event
            for event in views[child["id"]]["events"]
            if event["type"].startswith("agent.")
        )
    left, right = (views[child["id"]] for child in children)
    assert {event["event_id"] for event in left["events"]}.isdisjoint(
        event["event_id"] for event in right["events"]
    )
    run_ids = {event["event_id"] for event in query.by_task("expert", run["id"], owner_id="local-user")["events"]}
    assert {event["event_id"] for event in left["events"]} < run_ids
    with pytest.raises(TraceNotFound):
        query.by_task("expert", children[0]["id"], owner_id="other-user")
    _finish_turn(service, thread, turn)
    service.delete_thread(thread.id)
    with pytest.raises(TraceNotFound):
        query.by_task("expert", children[0]["id"], owner_id="local-user")


def _expert_db(database):
    from app.agents import AgentTaskService
    from app.behavior import BehaviorBundleService

    db = database
    bundle = BehaviorBundleService(db).ensure({"code": "test"})
    BehaviorBundleService(db).activate("stable", bundle.id, "one")
    service = ConversationService(db)
    thread = service.create_thread("expert")
    turn = service.accept_turn(thread.id, "one", "expert", [])
    tasks = AgentTaskService(db, thread_events=service.events)
    run = tasks.create_run(
        "local-user", "topic", {}, bundle.id, thread_id=thread.id,
        parent_turn_id=turn.turn_id, idempotency_key="one", append_thread_message=False,
    )
    return service, thread, turn, run


def _stored_envelope(db, thread_id: str) -> dict:
    with db.connection() as connection:
        raw = connection.execute(
            "SELECT envelope_json FROM thread_events WHERE thread_id=? AND envelope_json IS NOT NULL LIMIT 1",
            (thread_id,),
        ).fetchone()["envelope_json"]
    return json.loads(raw)


def _foreign_envelope(envelope: dict) -> str:
    body = json.loads(json.dumps(envelope))
    body["context"]["context"]["owner_id"] = "other-owner"
    return json.dumps(body)


def _insert_thread(db, thread_id, turn_id, rows: list[tuple]) -> None:
    """rows are (event_id, occurred_at, event_type, envelope_json or None)."""
    with db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=?", (thread_id,),
        ).fetchone()[0]
        for offset, (event_id, occurred_at, event_type, body) in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
                "VALUES (1,?,?,?,?,?,?,?,'{}',?)",
                (event_id, seq + offset, thread_id, turn_id, event_type, occurred_at, "runtime", body),
            )
        connection.execute("UPDATE threads SET next_event_seq=? WHERE id=?", (seq + len(rows) + 1, thread_id))


def _insert_agent(db, run_id, task_id, event_id, occurred_at) -> None:
    with db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM agent_events WHERE agent_run_id=?", (run_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO agent_events(event_id,agent_run_id,seq,task_id,type,actor,data_json,occurred_at) "
            "VALUES (?,?,?,?,'agent.note','worker','{}',?)",
            (event_id, run_id, seq, task_id, occurred_at),
        )


def _collect_thread(query, thread_id, limit):
    found = []
    cursor = None
    for _ in range(80):
        view = query.by_thread(
            thread_id, owner_id="local-user", limit=limit,
            **({} if cursor is None else {"cursor": cursor}),
        )
        ids = [event["event_id"] for event in view["events"]]
        assert len(ids) == len(set(ids))
        assert set(ids).isdisjoint(found)
        found.extend(ids)
        if view["next_cursor"] is None:
            return found, view
        assert view["next_cursor"] != cursor
        cursor = view["next_cursor"]
        assert query._last_thread_params[-1] <= limit + SOURCE_SEEK_BUDGET
    raise AssertionError("pagination did not terminate")


def test_refused_source_rows_do_not_skip_a_valid_event(database):
    """A source full of owner conflicts must be refilled before another source is chosen."""
    service, thread, turn, run = _expert_db(database)
    envelope = _stored_envelope(service.db, thread.id)
    foreign = _foreign_envelope(envelope)
    _insert_thread(service.db, thread.id, turn.turn_id, [
        ("conflict-a", "2020-01-01T00:00:00+00:00", "note.foreign", foreign),
        ("conflict-b", "2020-01-02T00:00:00+00:00", "note.foreign", foreign),
        ("must-appear", "2020-01-03T00:00:00+00:00", "note.kept", json.dumps(envelope)),
    ])
    _insert_agent(service.db, run["id"], run["coordinator_task_id"], "agent-later", "2020-01-04T00:00:00+00:00")
    query = TraceQuery(service.db)
    small, _last = _collect_thread(query, thread.id, 1)
    assert small.index("must-appear") < small.index("agent-later")
    assert "conflict-a" not in small and "conflict-b" not in small
    assert "other-owner" not in json.dumps(query.by_thread(thread.id, owner_id="local-user", limit=1000))
    large = [event["event_id"] for event in query.by_thread(thread.id, owner_id="local-user", limit=1000)["events"]]
    assert set(small) == set(large)


def test_duplicate_event_id_is_returned_once_across_pages(database):
    """The earliest sort key is the only representative, independent of page size."""
    service, thread, turn, run = _expert_db(database)
    envelope = json.dumps(_stored_envelope(service.db, thread.id))
    _insert_thread(service.db, thread.id, turn.turn_id, [
        ("duplicate-late", "2020-03-01T00:00:00+00:00", "note.dup", envelope),
        ("middle-a", "2020-06-01T00:00:00+00:00", "note.mid", envelope),
        ("middle-b", "2020-06-02T00:00:00+00:00", "note.mid", envelope),
    ])
    _insert_agent(
        service.db, run["id"], run["coordinator_task_id"], "duplicate-late", "2021-01-01T00:00:00+00:00",
    )
    query = TraceQuery(service.db)
    small, _last = _collect_thread(query, thread.id, 1)
    assert small.count("duplicate-late") == 1
    assert small.index("duplicate-late") < small.index("middle-a")
    large = [event["event_id"] for event in query.by_thread(thread.id, owner_id="local-user", limit=1000)["events"]]
    assert large.count("duplicate-late") == 1
    assert set(small) == set(large)


def test_refused_representative_hides_the_later_copy(database):
    """A later copy does not replace the representative when that row is refused."""
    service, thread, turn, run = _expert_db(database)
    envelope = _stored_envelope(service.db, thread.id)
    _insert_thread(service.db, thread.id, turn.turn_id, [
        ("shadowed", "2020-04-01T00:00:00+00:00", "note.foreign", _foreign_envelope(envelope)),
        ("kept-visible", "2020-04-02T00:00:00+00:00", "note.kept", json.dumps(envelope)),
    ])
    _insert_agent(service.db, run["id"], run["coordinator_task_id"], "shadowed", "2020-05-01T00:00:00+00:00")
    query = TraceQuery(service.db)
    small, _last = _collect_thread(query, thread.id, 1)
    assert "shadowed" not in small
    assert "kept-visible" in small
    large = [event["event_id"] for event in query.by_thread(thread.id, owner_id="local-user", limit=1000)["events"]]
    assert "shadowed" not in large
    assert set(small) == set(large)


def test_refusal_scan_budget_stays_resumable(database):
    service, thread, turn, run = _expert_db(database)
    envelope = _stored_envelope(service.db, thread.id)
    foreign = _foreign_envelope(envelope)
    width = 1 + SOURCE_SEEK_BUDGET + 5
    _insert_thread(service.db, thread.id, turn.turn_id, [
        (f"conflict-{index}", "2019-01-01T00:00:00+00:00", "note.foreign", foreign)
        for index in range(width)
    ] + [("must-appear", "2019-06-01T00:00:00+00:00", "note.kept", json.dumps(envelope))])
    _insert_agent(service.db, run["id"], run["coordinator_task_id"], "agent-later", "2019-12-01T00:00:00+00:00")
    query = TraceQuery(service.db)
    first = query.by_thread(thread.id, owner_id="local-user", limit=1)
    first_ids = [event["event_id"] for event in first["events"]]
    assert "must-appear" not in first_ids
    assert "agent-later" not in first_ids
    assert first["next_cursor"]
    assert any(reason["code"] == "scan_budget" for reason in first["completeness"]["reasons"])
    assert query._last_thread_params[-1] <= 1 + SOURCE_SEEK_BUDGET
    small, _last = _collect_thread(query, thread.id, 1)
    assert small.index("must-appear") < small.index("agent-later")
    assert not any(event_id.startswith("conflict-") for event_id in small)
    large = [event["event_id"] for event in query.by_thread(thread.id, owner_id="local-user", limit=1000)["events"]]
    assert set(small) == set(large)


def _like_miss_envelope(trace_id: str) -> str:
    """Passes the legacy LIKE probe and fails the exact envelope trace check."""
    return json.dumps({
        "schema_version": "1",
        "context": {"context": {"trace_id": trace_id, "note": "legacy-decoy-secret"}},
    }, separators=(",", ":"))


def _exact_legacy_envelope(thread_id: str, turn_id: str, trace_id: str) -> str:
    from dataclasses import replace
    from app.event_envelope import EventMetadata
    from app.execution_context import create_root_context
    context = replace(
        create_root_context(owner_id="local-user", thread_id=thread_id, turn_id=turn_id,
                            run_id=f"chat-turn:{turn_id}"),
        trace_id=trace_id,
    )
    return json.dumps(EventMetadata(context, "runtime", turn_id).to_dict())


def _insert_legacy_candidates(db, thread_id: str, turn_id: str, trace_id: str, count: int, *, target: bool) -> None:
    with db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=?", (thread_id,),
        ).fetchone()[0]
        miss = _like_miss_envelope(trace_id)
        label = trace_id[:4]
        rows = [
            (f"legacy-miss-{label}-{index:05d}", seq + index, thread_id, turn_id, "2019-01-01T00:00:00+00:00", miss)
            for index in range(1, count + 1)
        ]
        connection.cursor().executemany(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,"
            "data_json,envelope_json,trace_id) VALUES (1,?,?,?,?,'legacy.miss',?,'runtime','{}',?,NULL)",
            rows,
        )
        next_seq = seq + count
        if target:
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,"
                "data_json,envelope_json,trace_id) VALUES (1,?,?,?,?,'legacy.target',?,'runtime','{}',?,NULL)",
                ("tevt-legacy-5", next_seq + 1, thread_id, turn_id, "2021-06-01T00:00:00+00:00",
                 _exact_legacy_envelope(thread_id, turn_id, trace_id)),
            )
            next_seq += 1
        connection.execute("UPDATE threads SET next_event_seq=? WHERE id=?", (next_seq + 1, thread_id))


def test_http_legacy_cap_without_a_match_stays_resumable(database, tmp_path):
    """A full legacy window with no exact match is unfinished, not a missing trace."""
    runtime = _make_runtime(database, tmp_path, MockModelGateway())
    thread = runtime.conversation.create_thread("local")
    turn = runtime.conversation.accept_turn(thread.id, "one", "hello", [])
    trace_id = "cd" * 16
    _insert_legacy_candidates(
        runtime.db, thread.id, turn.turn_id, trace_id, LEGACY_SCAN_LIMIT, target=True,
    )
    query = TraceQuery(runtime.db)
    direct = query.by_trace(trace_id, owner_id="local-user")
    assert direct["events"] == []
    assert query._last_thread_params[-1] == LEGACY_SCAN_LIMIT
    client = TestClient(create_app(runtime=runtime))
    headers = {"host": "127.0.0.1:8000"}
    first = client.get(f"/api/traces/{trace_id}", headers=headers)
    assert first.status_code == 200
    body = first.json()
    assert body["events"] == []
    assert body["legacy_scan_truncated"] is True
    assert body["completeness"]["status"] == "partial"
    assert any(reason["code"] == "truncated" for reason in body["completeness"]["reasons"])
    assert body["next_cursor"]
    assert "legacy-decoy-secret" not in first.text
    assert "tevt-legacy-5" not in first.text

    followed = client.get(f"/api/traces/{trace_id}", headers=headers, params={"cursor": body["next_cursor"]})
    assert followed.status_code == 200
    found = [event["event_id"] for event in followed.json()["events"]]
    assert "tevt-legacy-5" in found
    assert followed.json()["next_cursor"] != body["next_cursor"]
    assert "legacy-decoy-secret" not in followed.text

    foreign = client.get(f"/api/traces/{trace_id}", headers={**headers, "x-owner-id": "other-owner"})
    assert foreign.status_code == 404
    assert foreign.json()["detail"] == "trace not found"
    assert "tevt-legacy-5" not in foreign.text
    assert "local-user" not in foreign.text


def test_finished_legacy_scan_without_a_match_is_missing(database, tmp_path):
    """Once the legacy scan is finished and nothing matched, the trace is absent."""
    runtime = _make_runtime(database, tmp_path, MockModelGateway())
    thread = runtime.conversation.create_thread("local")
    turn = runtime.conversation.accept_turn(thread.id, "one", "hello", [])
    trace_id = "ab" * 16
    _insert_legacy_candidates(runtime.db, thread.id, turn.turn_id, trace_id, 3, target=False)
    client = TestClient(create_app(runtime=runtime))
    headers = {"host": "127.0.0.1:8000"}
    missing = client.get(f"/api/traces/{trace_id}", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "trace not found"
    with pytest.raises(TraceNotFound):
        TraceQuery(runtime.db).by_trace(trace_id, owner_id="local-user")

    capped = "ef" * 16
    _insert_legacy_candidates(
        runtime.db, thread.id, turn.turn_id, capped, LEGACY_SCAN_LIMIT, target=False,
    )
    query = TraceQuery(runtime.db)
    first = query.by_trace(capped, owner_id="local-user")
    assert first["events"] == []
    assert first["legacy_scan_truncated"] is True
    assert first["next_cursor"]
    assert query._last_thread_params[-1] == LEGACY_SCAN_LIMIT
    with pytest.raises(TraceNotFound):
        query.by_trace(capped, owner_id="local-user", cursor=first["next_cursor"])
    hidden = client.get(f"/api/traces/{capped}", headers={**headers, "x-owner-id": "other-owner"})
    assert hidden.status_code == 404
    assert "legacy-miss-" not in hidden.text
