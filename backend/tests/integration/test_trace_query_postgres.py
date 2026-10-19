from __future__ import annotations

import json

import psycopg
import pytest

from app.conversation import ConversationService
from app.db import Database, POSTGRES_SCHEMA_HEAD
from app.trace_query import LEGACY_SCAN_LIMIT, SOURCE_SEEK_BUDGET, TraceNotFound, TraceQuery, _encode_cursor


def _finish(db, thread_id: str, turn_id: str) -> None:
    with db.transaction() as connection:
        connection.execute("UPDATE turns SET status='COMPLETED' WHERE id=?", (turn_id,))
        connection.execute("UPDATE threads SET active_turn_id=NULL WHERE id=?", (thread_id,))


def test_postgres_trace_schema_and_round_trip(migrated_postgres_url, tmp_path):
    with psycopg.connect(migrated_postgres_url, row_factory=psycopg.rows.dict_row) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == POSTGRES_SCHEMA_HEAD
        for table in ("events", "thread_events"):
            columns = connection.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name=%s AND column_name='trace_id'", (table,)
            ).fetchone()
            assert columns is not None
        indexes = {
            row["indexname"] for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema() "
                "AND indexname IN ('idx_events_trace_id','idx_thread_events_trace_id')"
            )
        }
        assert indexes == {"idx_events_trace_id", "idx_thread_events_trace_id"}

    service = ConversationService(Database(migrated_postgres_url, workspace=tmp_path / "workspace"))
    thread = service.create_thread("pg-trace")
    turn = service.accept_turn(thread.id, "one", "hello", [])
    query = TraceQuery(service.db)
    view = query.by_thread(thread.id, owner_id="local-user")
    trace_id = view["events"][0]["context"]["context"]["trace_id"]
    assert query.by_trace(trace_id, owner_id="local-user")["events"]
    _finish(service.db, thread.id, turn.turn_id)
    service.delete_thread(thread.id)
    with pytest.raises(TraceNotFound):
        query.by_trace(trace_id, owner_id="local-user")


def test_postgres_legacy_trace_filters_before_page_cap(migrated_postgres_url, tmp_path):
    service = ConversationService(Database(migrated_postgres_url, workspace=tmp_path / "workspace"))
    thread = service.create_thread("pg-legacy")
    turn = service.accept_turn(thread.id, "one", "hello", [])
    with service.db.connection() as connection:
        source = connection.execute(
            "SELECT envelope_json FROM thread_events WHERE thread_id=? ORDER BY seq LIMIT 1", (thread.id,)
        ).fetchone()["envelope_json"]
    envelope = json.loads(source)
    target_trace = "e" * 32
    envelope["context"]["context"]["trace_id"] = target_trace
    envelope = json.dumps(envelope, ensure_ascii=False)
    with service.db.transaction() as connection:
        next_seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM thread_events WHERE thread_id=?", (thread.id,)
        ).fetchone()[0]
        for index in range(8):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
                "VALUES (1,?,?,?,?,?,CURRENT_TIMESTAMP,?,'{}',NULL)",
                (f"pg-legacy-noise-{index}", next_seq + index, thread.id, turn.turn_id,
                 f"legacy.noise.{index}", "test"),
            )
        for index in range(3):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
                "VALUES (1,?,?,?,?,?,CURRENT_TIMESTAMP,?,'{}',?)",
                (f"pg-legacy-target-{index}", next_seq + 8 + index, thread.id, turn.turn_id,
                 "legacy.target", "test", envelope),
            )
    query = TraceQuery(service.db)
    ids = []
    cursor = None
    for index in range(3):
        view = query.by_trace(
            target_trace, owner_id="local-user", limit=1,
            **({} if cursor is None else {"cursor": cursor}),
        )
        assert view["count"] == 1
        ids.append(view["events"][0]["event_id"])
        cursor = view["next_cursor"]
        assert (cursor is None) is (index == 2)
    assert ids == [f"pg-legacy-target-{index}" for index in range(3)]


def test_postgres_expert_task_isolation_and_deleted_thread(migrated_postgres_url, tmp_path):
    from app.agents import AgentTaskService
    from app.behavior import BehaviorBundleService

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    bundles = BehaviorBundleService(db)
    bundle = bundles.ensure({"code": "test"})
    bundles.activate("stable", bundle.id, "one")
    service = ConversationService(db)
    thread = service.create_thread("pg-expert")
    turn = service.accept_turn(thread.id, "one", "expert", [])
    tasks = AgentTaskService(db, thread_events=service.events)
    runs = [tasks.create_run(
        "local-user", "topic", {}, bundle.id, thread_id=thread.id,
        parent_turn_id=turn.turn_id, idempotency_key=f"run-{index}", append_thread_message=False,
    ) for index in range(2)]
    for run in runs:
        service.events.append(thread.id, turn.turn_id, "expert.task.note", "worker", {
            "task_id": run["coordinator_task_id"], "agent_run_id": run["id"],
        })
    query = TraceQuery(db)
    for run in runs:
        run_view = query.by_task("expert", run["id"], owner_id="local-user")
        assert run_view["events"]
        assert all(event["payload"]["agent_run_id"] == run["id"] for event in run_view["events"])
        task_id = run["coordinator_task_id"]
        view = query.by_task("expert", task_id, owner_id="local-user")
        assert view["task_scope"] == "partial"
        kinds = {event["type"] for event in view["events"]}
        assert {"expert.task.note", "agent.run.created"} <= kinds
        assert all(event["payload"]["task_id"] == task_id for event in view["events"])
        with pytest.raises(TraceNotFound):
            query.by_task("expert", task_id, owner_id="foreign-user")
    _finish(db, thread.id, turn.turn_id)
    service.delete_thread(thread.id)
    for run in runs:
        for identifier in (run["id"], run["coordinator_task_id"]):
            with pytest.raises(TraceNotFound):
                query.by_task("expert", identifier, owner_id="local-user")


def test_postgres_task_query_limits_unrelated_history_and_explains(migrated_postgres_url, tmp_path):
    """100k unrelated rows stay in the database filter; the page is the task's own events."""
    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    service = ConversationService(db)
    thread = service.create_thread("bulk")
    turn = service.accept_turn(thread.id, "one", "hello", [])
    now = "2026-01-01T00:00:00+00:00"
    with db.transaction() as connection:
        connection.execute(
            "INSERT INTO research_jobs(id,thread_id,source_turn_id,trigger_kind,occurrence_key,topic,"
            "source_scopes_json,status,phase,available_at,created_at,updated_at) "
            "VALUES ('job-bulk',?,?,'manual','occ-bulk','topic','[\"web\"]','COMPLETED','completed',?,?,?)",
            (thread.id, turn.turn_id, now, now, now),
        )
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
        batch = [
            (f"noise-{index}", seq + index, thread.id, turn.turn_id, "2024-01-01T00:00:00+00:00")
            for index in range(1, 100_001)
        ]
        cursor = connection.cursor()
        statement = (
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json) "
            "VALUES (1,?,?,?,?,'noise',?,'runtime','{}')"
        )
        for start in range(0, len(batch), 5000):
            cursor.executemany(statement, batch[start:start + 5000])
        connection.execute(
            "UPDATE threads SET next_event_seq=? WHERE id=?", (seq + 100_001, thread.id),
        )
    service.events.append(thread.id, turn.turn_id, "research.target", "research_worker", {"job_id": "job-bulk"})
    query = TraceQuery(db)
    view = query.by_task("research", "job-bulk", owner_id="local-user", limit=10)
    assert [event["type"] for event in view["events"]] == ["research.target"]
    assert view["events"][0]["payload"]["job_id"] == "job-bulk"
    assert view["next_cursor"] is None
    assert "job_id" in query._last_thread_sql and "LIMIT" in query._last_thread_sql.upper()
    with db.connection() as connection:
        plan = connection.execute("EXPLAIN " + query._last_thread_sql, query._last_thread_params).fetchall()
    plan_text = "\n".join(str(row[0]) for row in plan)
    assert "Limit" in plan_text
    event = view["events"][0]
    parked = _encode_cursor(
        {"thread": 2_000_000_000, "run": 2_000_000_000, "agent": 2_000_000_000},
        (event["occurred_at"], event["stream_kind"], event["stream_id"], event["seq"], event["event_id"]),
    )
    empty = query.by_task("research", "job-bulk", owner_id="local-user", limit=10, cursor=parked)
    assert empty["events"] == []
    assert query._last_thread_params[-1] == 11


def test_postgres_page_merge_keeps_valid_rows_and_one_event_id(migrated_postgres_url, tmp_path):
    """Refusal refill, cross-page identity, and a resumable seek budget on PostgreSQL."""
    from app.agents import AgentTaskService
    from app.behavior import BehaviorBundleService

    db = Database(migrated_postgres_url, workspace=tmp_path / "workspace")
    bundles = BehaviorBundleService(db)
    bundle = bundles.ensure({"code": "pg-page"})
    bundles.activate("stable", bundle.id, "one")
    service = ConversationService(db)
    thread = service.create_thread("pg-page")
    turn = service.accept_turn(thread.id, "one", "page", [])
    tasks = AgentTaskService(db, thread_events=service.events)
    run = tasks.create_run(
        "local-user", "topic", {}, bundle.id, thread_id=thread.id,
        parent_turn_id=turn.turn_id, idempotency_key="pg-page", append_thread_message=False,
    )
    with db.connection() as connection:
        envelope = json.loads(connection.execute(
            "SELECT envelope_json FROM thread_events WHERE thread_id=? AND envelope_json IS NOT NULL LIMIT 1",
            (thread.id,),
        ).fetchone()["envelope_json"])
    foreign = json.loads(json.dumps(envelope))
    foreign["context"]["context"]["owner_id"] = "other-owner"
    foreign_body = json.dumps(foreign)
    kept = json.dumps(envelope)
    width = 1 + SOURCE_SEEK_BUDGET + 5
    rows = [
        (f"pg-conflict-{index}", "2019-01-01T00:00:00+00:00", "note.foreign", foreign_body)
        for index in range(width)
    ]
    rows.extend([
        ("pg-shadowed", "2019-02-01T00:00:00+00:00", "note.foreign", foreign_body),
        ("pg-must", "2019-06-01T00:00:00+00:00", "note.kept", kept),
        ("pg-duplicate", "2019-07-01T00:00:00+00:00", "note.dup", kept),
        ("pg-middle", "2019-08-01T00:00:00+00:00", "note.mid", kept),
    ])
    with db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=?", (thread.id,),
        ).fetchone()[0]
        for offset, (event_id, occurred_at, event_type, body) in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,data_json,envelope_json) "
                "VALUES (1,?,?,?,?,?,?,?,'{}',?)",
                (event_id, seq + offset, thread.id, turn.turn_id, event_type, occurred_at, "runtime", body),
            )
        connection.execute("UPDATE threads SET next_event_seq=? WHERE id=?", (seq + len(rows) + 1, thread.id))
        agent_seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM agent_events WHERE agent_run_id=?", (run["id"],),
        ).fetchone()[0]
        for offset, (event_id, occurred_at) in enumerate((
            ("pg-shadowed", "2019-02-02T00:00:00+00:00"),
            ("pg-duplicate", "2019-09-01T00:00:00+00:00"),
            ("pg-agent-later", "2019-12-01T00:00:00+00:00"),
        ), start=1):
            connection.execute(
                "INSERT INTO agent_events(event_id,agent_run_id,seq,task_id,type,actor,data_json,occurred_at) "
                "VALUES (?,?,?,?,'agent.note','worker','{}',?)",
                (event_id, run["id"], agent_seq + offset, run["coordinator_task_id"], occurred_at),
            )
    query = TraceQuery(db)
    first = query.by_thread(thread.id, owner_id="local-user", limit=1)
    first_ids = [event["event_id"] for event in first["events"]]
    assert "pg-must" not in first_ids and "pg-agent-later" not in first_ids
    assert first["next_cursor"]
    assert any(reason["code"] == "scan_budget" for reason in first["completeness"]["reasons"])
    found = []
    cursor = first["next_cursor"]
    found.extend(first_ids)
    for _ in range(120):
        view = query.by_thread(thread.id, owner_id="local-user", limit=1, cursor=cursor)
        ids = [event["event_id"] for event in view["events"]]
        assert set(ids).isdisjoint(found)
        found.extend(ids)
        assert query._last_thread_params[-1] <= 1 + SOURCE_SEEK_BUDGET
        if view["next_cursor"] is None:
            break
        assert view["next_cursor"] != cursor
        cursor = view["next_cursor"]
    else:
        raise AssertionError("pagination did not terminate")
    assert found.index("pg-must") < found.index("pg-agent-later")
    assert found.count("pg-duplicate") == 1
    assert found.index("pg-duplicate") < found.index("pg-middle")
    assert "pg-shadowed" not in found
    assert not any(event_id.startswith("pg-conflict-") for event_id in found)
    assert "other-owner" not in json.dumps(query.by_thread(thread.id, owner_id="local-user", limit=1000))
    large = [event["event_id"] for event in query.by_thread(thread.id, owner_id="local-user", limit=1000)["events"]]
    assert set(found) == set(large)


def test_postgres_legacy_cap_without_a_match_resumes(migrated_postgres_url, tmp_path):
    """The first 5000 LIKE hits are not the end of an isolated legacy trace."""
    service = ConversationService(Database(migrated_postgres_url, workspace=tmp_path / "workspace"))
    thread = service.create_thread("pg-legacy-cap")
    turn = service.accept_turn(thread.id, "one", "hello", [])
    trace_id = "cd" * 16
    miss = json.dumps({
        "schema_version": "1",
        "context": {"context": {"trace_id": trace_id, "note": "legacy-decoy-secret"}},
    }, separators=(",", ":"))
    with service.db.connection() as connection:
        source = connection.execute(
            "SELECT envelope_json FROM thread_events WHERE thread_id=? ORDER BY seq LIMIT 1", (thread.id,)
        ).fetchone()["envelope_json"]
    target_body = json.loads(source)
    target_body["context"]["context"]["trace_id"] = trace_id
    target = json.dumps(target_body, ensure_ascii=False)
    with service.db.transaction() as connection:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=?", (thread.id,)
        ).fetchone()[0]
        rows = [
            (f"pg-legacy-miss-{index:05d}", seq + index, thread.id, turn.turn_id, "2019-01-01T00:00:00+00:00", miss)
            for index in range(1, LEGACY_SCAN_LIMIT + 1)
        ]
        connection.cursor().executemany(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,"
            "data_json,envelope_json,trace_id) VALUES (1,?,?,?,?,'legacy.miss',?,'runtime','{}',?,NULL)",
            rows,
        )
        connection.execute(
            "INSERT INTO thread_events(schema_version,event_id,seq,thread_id,turn_id,type,occurred_at,actor,"
            "data_json,envelope_json,trace_id) VALUES (1,?,?,?,?,'legacy.target',?,'runtime','{}',?,NULL)",
            ("tevt-legacy-5", seq + LEGACY_SCAN_LIMIT + 1, thread.id, turn.turn_id,
             "2021-06-01T00:00:00+00:00", target),
        )
        connection.execute(
            "UPDATE threads SET next_event_seq=? WHERE id=?",
            (seq + LEGACY_SCAN_LIMIT + 2, thread.id),
        )
    query = TraceQuery(service.db)
    with pytest.raises(TraceNotFound):
        query.by_trace(trace_id, owner_id="other-owner")
    first = query.by_trace(trace_id, owner_id="local-user")
    assert first["events"] == []
    assert first["legacy_scan_truncated"] is True
    assert first["completeness"]["status"] == "partial"
    assert any(reason["code"] == "truncated" for reason in first["completeness"]["reasons"])
    assert first["next_cursor"]
    assert query._last_thread_params[-1] == LEGACY_SCAN_LIMIT
    followed = query.by_trace(trace_id, owner_id="local-user", cursor=first["next_cursor"])
    assert [event["event_id"] for event in followed["events"]] == ["tevt-legacy-5"]
    assert followed["next_cursor"] != first["next_cursor"]
    assert "legacy-decoy-secret" not in json.dumps(followed)
