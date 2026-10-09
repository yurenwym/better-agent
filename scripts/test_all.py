"""Required commit gate: entire backend suite (including isolated PostgreSQL), UI tests and build."""
from pathlib import Path
import subprocess
import sys
import shutil

root = Path(__file__).resolve().parents[1]
npm = shutil.which("npm")
if npm is None:
    raise SystemExit("npm is required for the full commit gate")
for cwd, command in (
    (root / "backend", [sys.executable, "-m", "pytest", "-q"]),
    (root / "frontend", [npm, "test", "--", "--run"]),
    (root / "frontend", [npm, "run", "build"]),
):
    result = subprocess.run(command, cwd=cwd)
    if result.returncode:
        raise SystemExit(result.returncode)
