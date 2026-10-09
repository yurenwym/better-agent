"""Small transport observers shared by snapshot entrypoint acceptance tests."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any


def _same_logical_request(snapshot: Any, request: Any) -> bool:
    frozen = snapshot.to_request()
    return all(getattr(frozen, name) == getattr(request, name) for name in (
        "messages", "tools", "temperature", "max_tokens", "thinking",
        "response_format", "single_attempt",
    ))


@dataclass
class CommittedSnapshotTransport:
    """Mock provider transport that audits the committed binding before reply.

    Reads through a fresh database connection at the transport boundary. It
    stores only opaque IDs/digests and profile linkage, never request text.
    """

    db: Any
    owner_id: str
    callsite_id: str
    response: Any
    observations: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, profile: Any, request: Any, **_kwargs: Any) -> Any:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT a.id AS attempt_id,a.invocation_id,a.profile_version_id,a.status AS attempt_status,"
                "i.owner_id,i.status AS invocation_status,i.context_snapshot_id,i.context_snapshot_digest,"
                "i.execution_context_digest "
                "FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id "
                "WHERE a.status='STARTED' ORDER BY a.started_at DESC,a.id DESC LIMIT 1",
            ).fetchone()
        if row is None:
            raise AssertionError("provider send has no committed attempt/invocation")
        if row["owner_id"] != self.owner_id or row["invocation_status"] != "RUNNING":
            raise AssertionError("provider send has an unexpected execution binding")
        if not row["context_snapshot_id"] or not row["context_snapshot_digest"]:
            raise AssertionError("provider send has no committed input snapshot binding")

        from app.model_input_snapshot_store import ModelInputSnapshotStore

        snapshot = ModelInputSnapshotStore(self.db).load(self.owner_id, row["context_snapshot_id"])
        if snapshot.content_digest != row["context_snapshot_digest"]:
            raise AssertionError("provider send snapshot digest differs from invocation binding")
        if not _same_logical_request(snapshot, request):
            raise AssertionError("provider request differs from the frozen logical input")

        provenance = snapshot.provenance()
        observation = {
            "callsite_id": self.callsite_id,
            "owner_id": row["owner_id"],
            "invocation_id": row["invocation_id"],
            "attempt_id": row["attempt_id"],
            "snapshot_id": row["context_snapshot_id"],
            "snapshot_digest": row["context_snapshot_digest"],
            "execution_digest": row["execution_context_digest"],
            "provenance_status": provenance.status,
            "provenance_sources": [
                {"kind": source.kind, "version": source.version, "included": source.included,
                 "location": source.location, "dropped_reason": source.dropped_reason}
                for source in provenance.sources
            ],
            "profile_version_id": row["profile_version_id"],
        }
        self.observations.append(observation)
        evidence_path = os.getenv("BETTER_SNAPSHOT_EVIDENCE_PATH")
        if evidence_path:
            path = Path(evidence_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(observation, ensure_ascii=False, sort_keys=True) + "\n")
        if isinstance(self.response, list):
            if not self.response:
                raise AssertionError("scripted transport ran out of responses")
            response = self.response.pop(0)
        else:
            response = self.response
        if isinstance(response, BaseException):
            raise response
        return response
