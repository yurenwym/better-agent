from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from app.startup import build_runtime


def main() -> int:
    samples: list[float] = []
    with tempfile.TemporaryDirectory(prefix="better-agent-benchmark-") as directory:
        runtime = build_runtime(Path(directory))
        for index in range(100):
            started = time.perf_counter()
            asyncio.run(runtime.create_goal(f"benchmark-{index}", "synthetic"))
            samples.append((time.perf_counter() - started) * 1000)
    ordered = sorted(samples)
    result = {"count": len(samples), "p50_ms": round(statistics.median(ordered), 3), "p95_ms": round(ordered[int(len(ordered) * .95) - 1], 3), "synthetic": True}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
