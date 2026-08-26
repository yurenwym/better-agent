from __future__ import annotations

import asyncio

from app.agents import AgentTaskService, ExpertAdvisoryService, ManagedAgentWorker
from app.behavior import BehaviorBundleService
from app.db import Database


class ExpertModel:
    async def execute(self, role, objective, context, inputs):
        return {
            "summary": f"{role} advice", "findings": [], "risks": [],
            "open_questions": [], "safety_pass": True,
        }


def test_mainflow_advisor_reuses_persistent_agent_tasks_and_is_idempotent(tmp_path):
    async def scenario():
        db = Database(tmp_path / "agent.db")
        bundles = BehaviorBundleService(db)
        bundle = bundles.ensure({"prompt": "v1"})
        bundles.activate("stable", bundle.id, "stable")
        tasks = AgentTaskService(db)
        worker = ManagedAgentWorker(tasks, ExpertModel(), poll_interval=.001)
        advisor = ExpertAdvisoryService(tasks, bundles, timeout_seconds=2, poll_interval=.001)
        await worker.start()
        try:
            first = await advisor.advise(
                purpose="plan", source_id="plan-version-1", objective="review plan",
                context={"plan": "bounded"}, roles=("planner", "critic"),
            )
            second = await advisor.advise(
                purpose="plan", source_id="plan-version-1", objective="review plan",
                context={"plan": "bounded"}, roles=("planner", "critic"),
            )
        finally:
            await worker.stop()
        with db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        return first, second, count

    first, second, count = asyncio.run(scenario())
    assert count == 1
    assert first == second
    assert [item["role"] for item in first["experts"]] == ["critic", "planner"]


def test_mainflow_advisor_fails_open_when_experts_are_unavailable(tmp_path):
    async def scenario():
        db = Database(tmp_path / "agent.db")
        bundles = BehaviorBundleService(db)
        bundle = bundles.ensure({"prompt": "v1"})
        bundles.activate("stable", bundle.id, "stable")
        tasks = AgentTaskService(db)
        worker = ManagedAgentWorker(tasks, None, poll_interval=.001)
        advisor = ExpertAdvisoryService(tasks, bundles, timeout_seconds=2, poll_interval=.001)
        await worker.start()
        try:
            return await advisor.advise(
                purpose="review", source_id="review-1", objective="review day",
                context={}, roles=("planner", "critic"),
            )
        finally:
            await worker.stop()

    assert asyncio.run(scenario()) is None
