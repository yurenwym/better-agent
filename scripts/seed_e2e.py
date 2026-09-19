from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.startup import build_runtime


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
    (data_root / "e2e-seed.json").write_text(
        json.dumps({"thread_id": thread.id, "plan_id": version.plan_document_id, "candidate_id": candidate["id"], "today": today.isoformat(), "tomorrow": tomorrow.isoformat()}),
        encoding="utf-8",
    )
    print(json.dumps({"thread_id": thread.id, "plan_id": version.plan_document_id, "today": today.isoformat()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
