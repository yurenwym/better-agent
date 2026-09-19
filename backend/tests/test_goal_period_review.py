import asyncio
import json

import pytest

from test_goal_programs import preview, service


class _PeriodCompiler:
    """只实现周期复盘的最小编译器替身，用来断言 evidence 的形状。"""

    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.evidence = None
        self.calls = 0

    async def period_review(self, evidence):
        self.calls += 1
        self.evidence = evidence
        return {"summary": self.summary}


class _FailingPeriodCompiler:
    async def period_review(self, evidence):
        from app.goal_program_compiler import GoalCompilationError

        raise GoalCompilationError("MODEL_UNAVAILABLE", "compiler unavailable", temporary=True)


def _completed_program(tmp_path):
    db, _, goals, version = service(tmp_path)
    draft = preview(goals, version)
    active = goals.activate(draft["id"], expected_version=draft["version"], idempotency_key="activate-1")
    for action in active["actions"]:
        goals.complete_action(action["id"], expected_version=0, idempotency_key=f"complete-{action['id']}")
    completed = goals.transition(draft["id"], "complete", expected_version=active["version"], idempotency_key="complete-program")
    return goals, completed


def test_period_summary_replaces_the_template_summary(tmp_path) -> None:
    goals, completed = _completed_program(tmp_path)
    # 切状态时先落一个确定性的模板总结作为兜底。
    assert completed["status"] == "COMPLETED"
    assert "必做行动完成" in completed["completion_summary"]

    compiler = _PeriodCompiler("这一周期强度安排合适，但恢复日偏少，下周把周三换成轻松骑行。")
    goals.compiler = compiler
    summary = asyncio.run(goals.period_summary(completed["id"]))

    assert summary == compiler.summary
    assert compiler.evidence["objective_title"] == completed["objective_title"]
    assert compiler.evidence["coach"] is None  # 历史结构没有 coach 时不应报错
    assert len(compiler.evidence["actions"]) == len(completed["actions"])
    assert compiler.evidence["daily_reviews"] == []
    assert asyncio.run(goals.period_summary(completed["id"])) == summary
    assert compiler.calls == 1

    goals.set_completion_summary(completed["id"], summary)
    assert goals.get(completed["id"])["completion_summary"] == summary


def test_period_summary_keeps_the_template_when_the_model_is_unavailable(tmp_path) -> None:
    goals, completed = _completed_program(tmp_path)
    goals.compiler = _FailingPeriodCompiler()

    assert asyncio.run(goals.period_summary(completed["id"])) is None
    assert "必做行动完成" in goals.get(completed["id"])["completion_summary"]


def test_failed_summary_retries_and_persists_result_atomically(tmp_path):
    goals, completed = _completed_program(tmp_path)
    goals.compiler = _FailingPeriodCompiler()
    assert asyncio.run(goals.period_summary(completed["id"])) is None
    assert goals.period_summary_status(completed["id"])["status"] == "FAILED"
    goals.compiler = _PeriodCompiler("Recovered summary")
    assert asyncio.run(goals.period_summary(completed["id"], retry_key="retry-1")) == "Recovered summary"
    assert goals.get(completed["id"])["completion_summary"] == "Recovered summary"
    with goals.db.connection() as connection:
        assert connection.execute("SELECT summary FROM memory_episodes WHERE id=?", (completed["completion_episode_id"],)).fetchone()[0] == "Recovered summary"
    assert asyncio.run(goals.period_summary(completed["id"], retry_key="retry-1")) == "Recovered summary"
    assert goals.compiler.calls == 1


def test_failed_retry_key_is_not_dispatched_twice(tmp_path):
    goals, completed = _completed_program(tmp_path)
    goals.compiler = _FailingPeriodCompiler()
    asyncio.run(goals.period_summary(completed["id"], retry_key="retry-1"))
    goals.compiler = _PeriodCompiler("Recovered")
    assert asyncio.run(goals.period_summary(completed["id"], retry_key="retry-1")) is None
    assert goals.compiler.calls == 0
    assert asyncio.run(goals.period_summary(completed["id"], retry_key="retry-2")) == "Recovered"


def test_period_timeout_is_visible_and_recoverable(tmp_path):
    goals, completed = _completed_program(tmp_path)
    class Slow:
        async def period_review(self, evidence):
            await asyncio.Event().wait()
    goals.compiler = Slow()
    goals.compile_timeout_seconds = .01
    assert asyncio.run(goals.period_summary(completed["id"])) is None
    state = goals.period_summary_status(completed["id"])
    assert state["status"] == "FAILED" and state["error_code"] == "MODEL_TIMEOUT"
    assert state["retryable"]
    goals.compiler = _PeriodCompiler("Recovered")
    assert asyncio.run(goals.period_summary(completed["id"], retry_key="retry")) == "Recovered"


@pytest.mark.asyncio
async def test_inflight_request_is_not_repeated_and_cancelled_request_can_recover(tmp_path):
    # Setup uses synchronous asyncio.run helpers outside the running loop.
    goals, completed = await asyncio.to_thread(_completed_program, tmp_path)
    started = asyncio.Event()
    class Waiting:
        async def period_review(self, evidence):
            started.set()
            await asyncio.Event().wait()
    goals.compiler = Waiting()
    task = asyncio.create_task(goals.period_summary(completed["id"]))
    await started.wait()
    assert await goals.period_summary(completed["id"], retry_key="busy") is None
    assert goals.period_summary_status(completed["id"])["status"] == "PENDING"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert goals.period_summary_status(completed["id"])["status"] == "UNKNOWN"
    goals.compiler = _PeriodCompiler("Recovered")
    assert await goals.period_summary(completed["id"]) is None
    assert await goals.period_summary(completed["id"], retry_key="recover") == "Recovered"


def test_legacy_pending_requires_explicit_retry_and_late_attempt_cannot_publish(tmp_path):
    goals, completed = _completed_program(tmp_path)
    goals.compiler = _FailingPeriodCompiler()
    asyncio.run(goals.period_summary(completed["id"]))
    with goals.db.transaction() as connection:
        row = connection.execute("SELECT * FROM goal_command_receipts WHERE operation='period-summary'").fetchone()
        old = json.loads(row["response_json"])
        connection.execute("UPDATE goal_command_receipts SET response_json=? WHERE id=?", ('{"status":"PENDING","summary":null}', row["id"]))
    assert goals.period_summary_status(completed["id"])["status"] == "UNKNOWN"
    goals.compiler = _PeriodCompiler("New result")
    assert asyncio.run(goals.period_summary(completed["id"])) is None
    assert asyncio.run(goals.period_summary(completed["id"], retry_key="recover")) == "New result"
    assert not goals._finish_period_summary(completed["id"], "local-user", row["idempotency_key"], row["request_hash"], old, "COMPLETED", summary="late")
    assert goals.get(completed["id"])["completion_summary"] == "New result"
