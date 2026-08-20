from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .plan_documents import content_hash, normalize_markdown


_DOCUMENT_ID = re.compile(r"^plan_[0-9a-f]{32}$")


class PlanFileSecurityError(ValueError):
    pass


class PlanFileConflict(ValueError):
    pass


class PlanFileProjector:
    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root).resolve()
        self.plans_root = self.data_root / "plans"
        self.plans_root.mkdir(parents=True, exist_ok=True)

    def path_for(self, document_id: str) -> Path:
        if not _DOCUMENT_ID.fullmatch(document_id):
            raise PlanFileSecurityError("invalid plan document id")
        document_dir = self.plans_root / document_id
        if document_dir.exists() and document_dir.is_symlink():
            raise PlanFileSecurityError("plan document directory cannot be a symlink")
        path = document_dir / "plan.md"
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.plans_root)
        except ValueError as exc:
            raise PlanFileSecurityError("plan path escapes data root") from exc
        return path

    def read_hash(self, document_id: str) -> str | None:
        path = self.path_for(document_id)
        if not path.exists():
            return None
        return content_hash(path.read_text(encoding="utf-8"))

    def project(
        self,
        document_id: str,
        content: str,
        *,
        expected_file_hash: str | None = None,
    ) -> str:
        normalized = normalize_markdown(content)
        path = self.path_for(document_id)
        current_hash = self.read_hash(document_id)
        if current_hash != expected_file_hash:
            raise PlanFileConflict("plan file hash conflict")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=".plan-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(normalized.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            target_hash = content_hash(path.read_text(encoding="utf-8"))
            if target_hash != content_hash(normalized):
                raise OSError("projected plan hash mismatch")
            return target_hash
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
