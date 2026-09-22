"""T00 基线快照：为「受测代码版本」生成可复现的内容指纹。

为什么需要它：本轮 V3 的十个核心文件**全部未跟踪**（`git cat-file -e HEAD:<path>` 失败），
所以 `HEAD` 不能充当受测代码版本。这里对每个受测文件算 SHA-256，任何验收报告只要引用
本 manifest，就能定位到实际跑过测试的那份字节。

用法：
    python backend/scripts/v3_baseline_manifest.py --out <本轮验收目录>/manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: 本轮验收产物是输出，不是受测代码；把它们排除在受测版本指纹之外，
#: 否则每写一份报告都会改变"受测版本"。
ACCEPTANCE_OUTPUT_PREFIX = "docs/acceptance/v3-optimization-2026-09-22/"


def is_acceptance_output(path: Path) -> bool:
    relative = str(path.relative_to(REPO)).replace("\\", "/")
    return relative.startswith(ACCEPTANCE_OUTPUT_PREFIX)


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def digest(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "lines": data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    head = git("rev-parse", "HEAD").strip()
    branch = git("symbolic-ref", "-q", "HEAD").strip()
    porcelain = [
        line for line in git("status", "--porcelain=v1").splitlines() if line.strip()
    ]

    tracked_modified = sorted(
        line[3:].strip() for line in porcelain if line[:2] == " M"
    )
    untracked = sorted(line[3:].strip() for line in porcelain if line[:2] == "??")

    # 目录型未跟踪条目展开成具体文件，否则 manifest 里只有一个目录名
    expanded: list[str] = []
    for entry in untracked:
        target = REPO / entry
        if target.is_dir():
            expanded.extend(
                str(p.relative_to(REPO)).replace("\\", "/")
                for p in sorted(target.rglob("*"))
                if p.is_file() and not is_acceptance_output(p)
            )
        elif not is_acceptance_output(target):
            expanded.append(entry)

    entries: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for rel in sorted(set(tracked_modified) | set(expanded)):
        path = REPO / rel
        if not path.is_file():
            missing.append(rel)
            continue
        info = digest(path)
        info["tracked_in_head"] = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{rel}"],
            cwd=REPO,
            capture_output=True,
        ).returncode == 0
        entries[rel] = info

    payload = {
        "generated_by": "backend/scripts/v3_baseline_manifest.py",
        "head": head,
        "branch": branch,
        "head_usable_as_tested_version": False,
        "reason_head_unusable": (
            "本轮 V3 核心文件未跟踪，HEAD 不含它们；受测版本以本 manifest 的 sha256 为准"
        ),
        "counts": {
            "tracked_modified": len(tracked_modified),
            "untracked_entries": len(untracked),
            "files_hashed": len(entries),
        },
        "tracked_modified": tracked_modified,
        "untracked_entries": untracked,
        "files": entries,
        "missing": missing,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"head={head} branch={branch}")
    print(
        f"hashed={len(entries)} modified={len(tracked_modified)} "
        f"untracked={len(untracked)} missing={len(missing)}"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
