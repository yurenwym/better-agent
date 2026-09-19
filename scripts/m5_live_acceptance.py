from __future__ import annotations

import runpy
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
runpy.run_path(str(BACKEND_ROOT / "scripts" / "m5_live_acceptance.py"), run_name="__main__")
