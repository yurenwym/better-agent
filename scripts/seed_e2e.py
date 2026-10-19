from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.event_envelope import EventMetadata
from app.execution_context import create_root_context
from app.startup import build_runtime
from app.trace_query import DEFAULT_LIMIT, SOURCE_SEEK_BUDGET

# Matches frontend TRACE_EMPTY_PAGE_BUDGET. Each empty page consumes one seek window.
EMPTY_PAGE_BUDGET = 3
FOREIGN_THREAD_ID = "thread_trace_foreign"
RESUME_THREAD_ID = "thread_trace_resume"
TRACE_TOKEN = "ghp_E2ETraceToken123"


def main() -> None:
    data_root = Path(os.getenv("BETTER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
    runtime = build_runtime(data_root)
    thread = runtime.conversation.create_thread("黄金闭环测试")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    tomorrow = today + timedelta(days=1)
    version = runtime.plan_documents.save_model_revision(
        thread_id=thread.id,
        title="两天行动计划",
        markdown_content="# 两天行动计划\n\n## 目标\n用两天完成一个可验证的小目标。\n\n## 每日安排\n- 每天完成一个行动并记录结果。\n",
        source_turn_id=None,
        source_message_id=None,
        actor="user",
    )
    # Display-redaction fixture only. This is not evidence that a tool ran.
    runtime.conversation.events.append(
        thread.id, f"plan_{thread.id}", "tool.execution.finished", "worker",
        {"name": TRACE_TOKEN, "ok": True},
    )
    for width in (1440,375):
        case_thread=runtime.conversation.create_thread(f"Calendar acceptance {width}")
        runtime.plan_documents.save_model_revision(thread_id=case_thread.id,title=f"Calendar acceptance {width}",markdown_content=f"# Calendar acceptance {width}\nOne action per available day.",source_turn_id=None,source_message_id=None,actor="user")
    base = runtime.behavior.active("stable")
    for index in range(3):
        run = __import__("asyncio").run(runtime.create_goal(f"E2E failure {index}", "observer candidate"))
        runtime.events.append(run.id, run.goal_id, "run.failed", "e2e", {"reason": "repeated prompt failure"})
    runtime.observer.observe()
    candidate = runtime.candidate_generator.generate()[0]
    runtime.evolution.evaluate(
        candidate["id"], expected_version=candidate["version"], deterministic_checks={"schema": True, "safety": True},
        metrics={"passed": 2, "total": 2, "baseline_correct": 1, "candidate_correct": 2, "quality_delta": 1, "safety_violations": 0},
        eval_set_digest="e2e-eval-set", evaluator_digest="e2e-deterministic", idempotency_key="e2e-evaluation",
    )
    _seed_trace_threads(runtime)
    (data_root / "e2e-seed.json").write_text(
        json.dumps({
            "thread_id": thread.id, "plan_id": version.plan_document_id, "candidate_id": candidate["id"],
            "today": today.isoformat(), "tomorrow": tomorrow.isoformat(),
            "foreign_thread_id": FOREIGN_THREAD_ID, "resume_thread_id": RESUME_THREAD_ID,
        }),
        encoding="utf-8",
    )
    print(json.dumps({"thread_id": thread.id, "plan_id": version.plan_document_id, "today": today.isoformat()}, ensure_ascii=False))


def _mismatch_envelope(thread_id: str) -> tuple[str, str]:
    metadata = EventMetadata(create_root_context(owner_id="other-user", thread_id=thread_id), "e2e", "resume-hidden")
    body = metadata.to_dict()
    return json.dumps(body, ensure_ascii=False), body["context"]["context"]["trace_id"]


def _seed_trace_threads(runtime) -> None:
    """Cross-user thread, plus a local thread whose first pages are not readable.

    The hidden rows carry another owner's envelope, so the production query omits
    them and returns an empty page with a cursor. The last row is a normal legacy
    event and appears only after the page continues.
    """
    now = datetime.now(ZoneInfo("UTC")).isoformat()
    hidden = (DEFAULT_LIMIT + SOURCE_SEEK_BUDGET) * EMPTY_PAGE_BUDGET
    with runtime.db.transaction() as connection:
        connection.execute(
            "INSERT INTO threads(id, title, owner_id, version, next_event_seq, created_at, updated_at) VALUES (?,?,?,0,1,?,?)",
            (FOREIGN_THREAD_ID, "其他用户轨迹", "other-user", now, now),
        )
        connection.execute(
            "INSERT INTO threads(id, title, owner_id, version, next_event_seq, created_at, updated_at) VALUES (?,?,?,0,1,?,?)",
            (RESUME_THREAD_ID, "轨迹续读", "local-user", now, now),
        )
        runtime.conversation.events.append(
            FOREIGN_THREAD_ID, "turn_foreign", "tool.execution.finished", "worker",
            {"name": "foreign-only-marker", "ok": True}, connection=connection,
            event_id="tevt_foreign_marker",
        )
        started = datetime(2026, 10, 1, tzinfo=ZoneInfo("UTC"))
        for index in range(hidden):
            envelope, trace_id = _mismatch_envelope(RESUME_THREAD_ID)
            occurred_at = (started + timedelta(seconds=index)).isoformat()
            connection.execute(
                "INSERT INTO thread_events(schema_version, event_id, seq, thread_id, turn_id, type, occurred_at, actor, data_json, envelope_json, trace_id) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"tevt_resume_{index:04d}", index + 1, RESUME_THREAD_ID, "turn_resume",
                    "approval.rejected", occurred_at, "user", json.dumps({"index": index}),
                    envelope, trace_id,
                ),
            )
        target_at = (started + timedelta(seconds=hidden)).isoformat()
        connection.execute(
            "INSERT INTO thread_events(schema_version, event_id, seq, thread_id, turn_id, type, occurred_at, actor, data_json) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "tevt_resume_target", hidden + 1, RESUME_THREAD_ID, "turn_resume",
                "plan.document_ready", target_at, "worker",
                json.dumps({
                    "plan_document_id": "resume-plan", "version_id": "resume-v9", "version": 9,
                    "content_hash": "resume", "source_message_id": None, "actor": "user",
                }),
            ),
        )
        connection.execute(
            "UPDATE threads SET next_event_seq = ? WHERE id = ?",
            (hidden + 2, RESUME_THREAD_ID),
        )


if __name__ == "__main__":
    main()
