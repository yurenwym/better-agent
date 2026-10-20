"""Explicit recovery for notification sends on this machine's execution scope.

This command opens the database and the notification service only. It does not
build the application runtime, start a worker, or load a model or provider secret.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .db import Database
from .notifications import NotificationService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover interrupted notification sends for this local scope")
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    db = Database(url, workspace=args.workspace)
    try:
        report = NotificationService(db).recover_interrupted_sends()
    finally:
        db.close()
    json.dump(
        {
            "recovered": [{"id": item.id, "status": item.status} for item in report.recovered],
            "retained": [{"id": item_id, "reason": reason} for item_id, reason in report.retained],
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
