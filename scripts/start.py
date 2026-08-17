from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main() -> None:
    subprocess.run(["npm", "run", "build"], cwd=FRONTEND, check=True)
    sys.path.insert(0, str(BACKEND))
    import uvicorn

    from app.main import create_app
    from app.startup import build_runtime

    app = create_app(runtime=build_runtime(ROOT / "data"), static_dir=FRONTEND / "dist")
    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
