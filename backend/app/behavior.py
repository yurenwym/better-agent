from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class BehaviorBundle:
    id: str
    bundle_hash: str
    manifest_json: str
    created_at: str

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_json)


class BehaviorBundleService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def ensure(self, manifest: dict[str, Any]) -> BehaviorBundle:
        if "task_policy" in manifest:
            from .task_policy import validate_task_policy
            validate_task_policy(manifest["task_policy"])
        canonical = _json(manifest)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM runtime_bundles WHERE bundle_hash=?", (digest,)).fetchone()
            if row is None:
                bundle_id = f"bundle_{digest[:24]}"
                connection.execute(
                    "INSERT INTO runtime_bundles(id,bundle_hash,manifest_json,created_at) VALUES (?,?,?,?)",
                    (bundle_id, digest, canonical, _now()),
                )
                row = connection.execute("SELECT * FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone()
        return _bundle(row)

    def get(self, bundle_id: str) -> BehaviorBundle:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone()
        if row is None:
            raise KeyError(bundle_id)
        return _bundle(row)

    def active(self, channel: str = "stable") -> BehaviorBundle:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT b.* FROM runtime_channels c JOIN runtime_bundles b ON b.id=c.bundle_id WHERE c.name=?", (channel,)
            ).fetchone()
        if row is None:
            raise KeyError(channel)
        return _bundle(row)

    def activate(self, channel: str, bundle_id: str, idempotency_key: str) -> BehaviorBundle:
        now = _now()
        with self.db.transaction() as connection:
            prior = connection.execute("SELECT to_bundle_id FROM runtime_channel_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior:
                if prior["to_bundle_id"] != bundle_id:
                    raise ValueError("idempotency key payload changed")
                return self.get(bundle_id)
            if connection.execute("SELECT 1 FROM runtime_bundles WHERE id=?", (bundle_id,)).fetchone() is None:
                raise KeyError(bundle_id)
            current = connection.execute("SELECT bundle_id FROM runtime_channels WHERE name=?", (channel,)).fetchone()
            connection.execute(
                "INSERT INTO runtime_channels(name,bundle_id,version,updated_at) VALUES (?,?,0,?) "
                "ON CONFLICT(name) DO UPDATE SET bundle_id=excluded.bundle_id,version=runtime_channels.version+1,updated_at=excluded.updated_at",
                (channel, bundle_id, now),
            )
            connection.execute(
                "INSERT INTO runtime_channel_events(id,channel_name,from_bundle_id,to_bundle_id,idempotency_key,created_at) VALUES (?,?,?,?,?,?)",
                (f"channel_event_{uuid.uuid4().hex}", channel, current["bundle_id"] if current else None, bundle_id, idempotency_key, now),
            )
        return self.get(bundle_id)


def _bundle(row: Any) -> BehaviorBundle:
    return BehaviorBundle(row["id"], row["bundle_hash"], row["manifest_json"], row["created_at"])
