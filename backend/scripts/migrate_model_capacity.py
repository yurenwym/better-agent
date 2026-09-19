"""T10 CLI: dry-run / apply the model capacity migration.

Usage (from backend/):

    python scripts/migrate_model_capacity.py --db data/agent.db
    python scripts/migrate_model_capacity.py --db data/agent.db --apply
    python scripts/migrate_model_capacity.py --db data/agent.db --apply --prefer-auto

Default keeps every existing explicit window as a manual cap. ``--prefer-auto``
only follows the catalog when its context integer is verified; otherwise the
report says why the manual window was kept.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Database  # noqa: E402
from app.model_capacity_migration import (  # noqa: E402
    apply_model_capacity_migration,
    plan_model_capacity_migration,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate model profiles onto the capacity contract")
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    parser.add_argument("--owner-id", default="local-user")
    parser.add_argument("--apply", action="store_true", help="create new versions (default: dry run)")
    parser.add_argument(
        "--prefer-auto", action="store_true",
        help="follow the catalog only when its capacity is verified",
    )
    args = parser.parse_args(argv)

    db = Database(Path(args.db))
    plans = plan_model_capacity_migration(db, owner_id=args.owner_id, prefer_auto=args.prefer_auto)
    report = apply_model_capacity_migration(
        db, plans, owner_id=args.owner_id, apply=args.apply,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
