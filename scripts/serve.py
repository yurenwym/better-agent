from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn

from app.main import create_app
from app.startup import build_runtime


def main() -> None:
    data_root = Path(os.getenv("BETTER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
    runtime = build_runtime(data_root)
    if os.getenv("BETTER_AGENT_E2E_POSTGRES") == "1" and os.getenv("BETTER_AGENT_E2E_DATA_ROOT"):
        from app.goal_program_compiler import FixedGoalProgramCompiler
        async def compile_program(markdown,request):
            result=await FixedGoalProgramCompiler().compile(markdown,request)
            for action in result["actions"]:action["estimated_minutes"]=30
            return result
        runtime.goal_programs.compiler.compile=compile_program
        runtime.goal_programs.compiler.review=FixedGoalProgramCompiler().review
    if os.getenv("BETTER_AGENT_E2E_SCRIPTED") == "1":
        from e2e_scripted_model import execute_attempt
        gateway = getattr(runtime.model, "gateway", None)
        if gateway is None:
            raise RuntimeError("scripted e2e server has no model gateway")
        gateway._execute_attempt = execute_attempt
        release_gate = data_root / "e2e-archive-release"
        if release_gate.exists():
            release_gate.unlink()
    if os.getenv("BETTER_AGENT_GOAL_REVIEW_DELAY") is not None:
        runtime.goal_reviews.queue_delay_seconds = max(0, int(os.environ["BETTER_AGENT_GOAL_REVIEW_DELAY"]))
    app = create_app(runtime=runtime, static_dir=ROOT / "frontend" / "dist")
    if os.getenv("BETTER_AGENT_E2E_SCRIPTED") == "1":
        @app.post("/api/e2e/archive-release")
        def release_archive() -> dict[str, bool]:
            (data_root / "e2e-archive-release").write_text("release", encoding="utf-8")
            return {"ok": True}

        # The frontend mount matches every path, so this route has to be first.
        app.router.routes.insert(0, app.router.routes.pop())
    host = os.getenv("BETTER_AGENT_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    if os.getenv("BETTER_AGENT_E2E_POSTGRES") == "1" and os.getenv("BETTER_AGENT_E2E_DATA_ROOT"):
        _install_e2e_shutdown(app)
        config = uvicorn.Config(app, host=host, port=port)
        server = uvicorn.Server(config)
        app.state.e2e_server = server
        pid_path = _write_e2e_pid()
        try:
            server.run()
        finally:
            if pid_path is not None:
                pid_path.unlink(missing_ok=True)
        return
    uvicorn.run(app, host=host, port=port)


def _install_e2e_shutdown(app) -> None:
    """Let teardown finish the existing server lifespan before the database stops."""

    @app.post("/api/e2e/shutdown")
    def shutdown_e2e(request: Request) -> dict[str, bool]:
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1"}:
            raise HTTPException(status_code=403, detail="shutdown is loopback only")
        server = getattr(request.app.state, "e2e_server", None)
        if server is None:
            raise HTTPException(status_code=503, detail="server is not ready")
        server.should_exit = True
        return {"ok": True}

    # The frontend mount matches every path, so this route has to be first.
    app.router.routes.insert(0, app.router.routes.pop())


def _write_e2e_pid() -> Path | None:
    root = os.environ.get("BETTER_AGENT_E2E_PG_ROOT", "").strip()
    role = os.environ.get("BETTER_AGENT_E2E_ROLE", "").strip()
    if not root or role not in {"plain", "scripted"}:
        return None
    path = Path(root) / f"{role}.pid"
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


if __name__ == "__main__":
    main()
