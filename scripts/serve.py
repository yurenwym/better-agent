from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn

from app.main import create_app
from app.startup import build_runtime


def main() -> None:
    data_root = Path(os.getenv("BETTER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
    runtime = build_runtime(data_root)
    if os.getenv("BETTER_AGENT_TEST_ALLOW_SQLITE")=="1" and os.getenv("BETTER_AGENT_E2E_DATA_ROOT"):
        from app.goal_program_compiler import FixedGoalProgramCompiler
        async def compile_program(markdown,request):
            result=await FixedGoalProgramCompiler().compile(markdown,request)
            for action in result["actions"]:action["estimated_minutes"]=30
            return result
        runtime.goal_programs.compiler.compile=compile_program
    if os.getenv("BETTER_AGENT_GOAL_REVIEW_DELAY") is not None:
        runtime.goal_reviews.queue_delay_seconds = max(0, int(os.environ["BETTER_AGENT_GOAL_REVIEW_DELAY"]))
    app = create_app(runtime=runtime, static_dir=ROOT / "frontend" / "dist")
    uvicorn.run(app, host=os.getenv("BETTER_AGENT_HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
