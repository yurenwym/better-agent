from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def npm_command() -> str:
    return "npm.cmd" if sys.platform == "win32" else "npm"


def main() -> None:
    sys.path.insert(0, str(BACKEND))
    from app.config import load_env_file, load_user_model_environment

    load_env_file(ROOT / ".env")
    load_user_model_environment()
    subprocess.run([npm_command(), "run", "build"], cwd=FRONTEND, check=True)
    import uvicorn

    from app.main import create_app
    from app.startup import build_runtime

    data_root = Path(os.getenv("BETTER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
    app = create_app(runtime=build_runtime(data_root), static_dir=FRONTEND / "dist")
    uvicorn.run(
        app,
        host=os.getenv("BETTER_AGENT_HOST", DEFAULT_HOST),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
    )


if __name__ == "__main__":
    main()
