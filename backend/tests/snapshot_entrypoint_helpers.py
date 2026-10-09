"""Small transport observers shared by snapshot entrypoint acceptance tests."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import uuid
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
    send_count: int = 0

    def record(self, profile: Any, request: Any) -> None:
        self.send_count += 1
        observation = {
            "send_id": uuid.uuid4().hex,
            "batch_id": os.getenv("BETTER_SNAPSHOT_BATCH_ID", "unbatched"),
            "pytest_node": os.getenv("PYTEST_CURRENT_TEST", "").removesuffix(" (call)"),
            "callsite_id": self.callsite_id,
            "valid_committed_binding": False,
        }
        try:
            observation.update(self._audit(profile, request))
            observation["valid_committed_binding"] = True
            self.observations.append(observation)
        finally:
            # Include failed audits in the denominator, never only valid rows.
            evidence_path = os.getenv("BETTER_SNAPSHOT_EVIDENCE_PATH")
            if evidence_path:
                path = Path(evidence_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(observation, ensure_ascii=False, sort_keys=True) + "\n")
    async def __call__(self, profile: Any, request: Any, **_kwargs: Any) -> Any:
        self.record(profile, request)
        if isinstance(self.response, list):
            if not self.response:
                raise AssertionError("scripted transport ran out of responses")
            response = self.response.pop(0)
        else:
            response = self.response
        if isinstance(response, BaseException):
            raise response
        return response

    def _audit(self, profile: Any, request: Any) -> dict[str, Any]:
        from app.model_input_snapshot_store import ModelInputSnapshotStore

        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT a.id AS attempt_id,a.invocation_id,a.profile_version_id,a.status AS attempt_status,"
                "i.owner_id,i.status AS invocation_status,i.context_snapshot_id,i.context_snapshot_digest,"
                "i.execution_context_digest "
                "FROM model_attempts a JOIN model_invocations i ON i.id=a.invocation_id "
                "WHERE a.status='STARTED' AND i.owner_id=? AND a.profile_version_id=?",
                (self.owner_id, getattr(profile, "registered_profile_version_id", None)),
            ).fetchall()
        if not rows:
            raise AssertionError("provider send has no committed attempt/invocation")
        matches = []
        for candidate in rows:
            if not candidate["context_snapshot_id"]:
                continue
            frozen = ModelInputSnapshotStore(self.db).load(self.owner_id, candidate["context_snapshot_id"])
            if frozen.role == request.role and frozen.purpose == request.purpose and _same_logical_request(frozen, request):
                matches.append((candidate, frozen))
        if len(matches) != 1:
            raise AssertionError("provider send binding is missing or ambiguous")
        row, snapshot = matches[0]
        if row["owner_id"] != self.owner_id or row["invocation_status"] != "RUNNING":
            raise AssertionError("provider send has an unexpected execution binding")
        if not row["context_snapshot_id"] or not row["context_snapshot_digest"]:
            raise AssertionError("provider send has no committed input snapshot binding")

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
        return observation
