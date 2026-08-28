from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .db import Database


MAX_ZIP_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 25 * 1024 * 1024
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SKILL_DOCUMENT_BYTES = 256 * 1024
ALLOWED_PHASES = {"conversation", "ask", "planner", "executor", "reflector", "researcher", "expert", "coordinator"}


class SkillValidationError(ValueError):
    pass


@dataclass(frozen=True)
class _Preview:
    package: bytes
    manifest: dict[str, Any]
    manifest_digest: str
    package_digest: str
    content: str


class SkillPlatform:
    def __init__(self, db: Database, root: str | Path, owner_id: str = "local-user") -> None:
        self.db = db
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner_id = owner_id
        self._previews: dict[str, _Preview] = {}

    def preview_install(self, package: bytes) -> dict[str, Any]:
        preview = self._validate_package(package)
        name, version = preview.manifest["name"], preview.manifest["version"]
        with self.db.connection() as connection:
            prior = connection.execute(
                "SELECT v.package_digest FROM skill_versions v JOIN skills s ON s.id=v.skill_id "
                "WHERE s.owner_id=? AND s.name=? AND v.version=?",
                (self.owner_id, name, version),
            ).fetchone()
        if prior and prior["package_digest"] != preview.package_digest:
            raise SkillValidationError("same skill version has different content")
        token = f"skill_install_{uuid.uuid4().hex}"
        self._previews[token] = preview
        return {
            "install_token": token,
            "name": name,
            "version": version,
            "title": preview.manifest["title"],
            "description": preview.manifest["description"],
            "requested_tools": preview.manifest["requested_tools"],
            "connectors": preview.manifest["connectors"],
            "phases": preview.manifest["phases"],
            "manifest_digest": preview.manifest_digest,
            "package_digest": preview.package_digest,
        }

    def confirm_install(self, install_token: str, *, granted_tools: list[str], idempotency_key: str) -> dict[str, Any]:
        preview = self._previews.get(install_token)
        if preview is None:
            raise SkillValidationError("install preview expired or missing")
        requested = set(preview.manifest["requested_tools"])
        if not isinstance(granted_tools, list) or any(not isinstance(item, str) for item in granted_tools):
            raise SkillValidationError("granted tools must be strings")
        granted = sorted(set(granted_tools))
        if not set(granted).issubset(requested):
            raise SkillValidationError("grant exceeds requested tools")
        request_digest = _digest({"package": preview.package_digest, "granted": granted})
        with self.db.transaction() as connection:
            cached = connection.execute(
                "SELECT data_json FROM skill_events WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if cached:
                data = json.loads(cached["data_json"])
                if data.get("request_digest") != request_digest:
                    raise SkillValidationError("idempotency key binding changed")
                return self.version(data["version_id"], connection=connection)
            now = _now()
            name, version = preview.manifest["name"], preview.manifest["version"]
            skill = connection.execute(
                "SELECT * FROM skills WHERE owner_id=? AND name=?", (self.owner_id, name)
            ).fetchone()
            skill_id = skill["id"] if skill else f"skill_{uuid.uuid4().hex}"
            if skill is None:
                connection.execute(
                    "INSERT INTO skills(id,owner_id,name,status,created_at,updated_at) VALUES (?,?,?,'ENABLED',?,?)",
                    (skill_id, self.owner_id, name, now, now),
                )
            elif skill["status"] == "UNINSTALLED":
                connection.execute("UPDATE skills SET status='ENABLED',uninstalled_at=NULL,updated_at=? WHERE id=?", (now, skill_id))
            prior = connection.execute(
                "SELECT * FROM skill_versions WHERE skill_id=? AND version=?", (skill_id, version)
            ).fetchone()
            if prior and prior["package_digest"] != preview.package_digest:
                raise SkillValidationError("same skill version has different content")
            version_id = prior["id"] if prior else f"skill_version_{uuid.uuid4().hex}"
            storage = self.root / name / version / preview.package_digest
            if prior is None:
                self._extract(preview.package, storage)
                connection.execute(
                    "INSERT INTO skill_versions(id,skill_id,version,package_digest,manifest_digest,manifest_json,title,description,content,"
                    "requested_tools_json,connectors_json,phases_json,storage_path,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ENABLED',?)",
                    (
                        version_id, skill_id, version, preview.package_digest, preview.manifest_digest,
                        _json(preview.manifest), preview.manifest["title"], preview.manifest["description"], preview.content,
                        _json(preview.manifest["requested_tools"]), _json(preview.manifest["connectors"]),
                        _json(preview.manifest["phases"]), str(storage), now,
                    ),
                )
            else:
                connection.execute("UPDATE skill_versions SET status='ENABLED' WHERE id=?", (version_id,))
            grant_digest = _digest(granted)
            grant = connection.execute(
                "SELECT id FROM skill_grants WHERE owner_id=? AND skill_version_id=?", (self.owner_id, version_id)
            ).fetchone()
            if grant:
                connection.execute(
                    "UPDATE skill_grants SET granted_tools_json=?,grant_digest=?,status='ACTIVE',version=version+1,updated_at=? WHERE id=?",
                    (_json(granted), grant_digest, now, grant["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO skill_grants(id,owner_id,skill_version_id,granted_tools_json,grant_digest,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,'ACTIVE',?,?)",
                    (f"skill_grant_{uuid.uuid4().hex}", self.owner_id, version_id, _json(granted), grant_digest, now, now),
                )
            connection.execute(
                "UPDATE skills SET status='ENABLED',default_version_id=?,updated_at=? WHERE id=?",
                (version_id, now, skill_id),
            )
            self._event(connection, skill_id, version_id, "skill.installed", {
                "version_id": version_id, "package_digest": preview.package_digest, "request_digest": request_digest,
            }, idempotency_key)
            return self.version(version_id, connection=connection)

    def list(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT v.id FROM skill_versions v JOIN skills s ON s.id=v.skill_id WHERE s.owner_id=? ORDER BY s.name,v.version",
                (self.owner_id,),
            ).fetchall()
            return [self.version(row["id"], connection=connection) for row in rows]

    def enabled_versions(self) -> list[dict[str, Any]]:
        return [item for item in self.list() if item["status"] == "ENABLED"]

    def default_version(self, name: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT default_version_id FROM skills WHERE owner_id=? AND name=? AND status='ENABLED'",
                (self.owner_id, name),
            ).fetchone()
        if row is None or not row["default_version_id"]:
            raise KeyError(name)
        return self.version(row["default_version_id"])

    def bootstrap_builtin(self, name: str, title: str, description: str, content: str, requested_tools: list[str], phases: list[str]) -> dict[str, Any]:
        patch = int(hashlib.sha256(content.encode("utf-8")).hexdigest()[:8], 16)
        manifest = {
            "schema_version": 1, "name": name, "version": f"1.0.{patch}", "title": title,
            "description": description, "requested_tools": requested_tools, "connectors": [],
            "phases": phases, "entry_document": "SKILL.md",
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, value in (("skill.json", _json(manifest)), ("SKILL.md", content)):
                info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, value)
        preview = self.preview_install(buffer.getvalue())
        return self.confirm_install(
            preview["install_token"], granted_tools=requested_tools,
            idempotency_key=f"builtin:{name}:{preview['package_digest']}",
        )

    def version(self, version_id: str, *, connection: Any | None = None) -> dict[str, Any]:
        if connection is None:
            with self.db.connection() as owned:
                return self.version(version_id, connection=owned)
        row = connection.execute(
            "SELECT v.*,s.name,s.owner_id,s.status skill_status,g.granted_tools_json,g.grant_digest,g.status grant_status "
            "FROM skill_versions v JOIN skills s ON s.id=v.skill_id "
            "LEFT JOIN skill_grants g ON g.skill_version_id=v.id AND g.owner_id=s.owner_id "
            "WHERE v.id=? AND s.owner_id=?",
            (version_id, self.owner_id),
        ).fetchone()
        if row is None:
            raise KeyError(version_id)
        return {
            "skill_id": row["skill_id"], "version_id": row["id"], "name": row["name"], "version": row["version"],
            "title": row["title"], "description": row["description"], "content": row["content"],
            "package_digest": row["package_digest"], "manifest_digest": row["manifest_digest"],
            "requested_tools": json.loads(row["requested_tools_json"]),
            "granted_tools": json.loads(row["granted_tools_json"] or "[]"),
            "connectors": json.loads(row["connectors_json"]), "phases": json.loads(row["phases_json"]),
            "grant_digest": row["grant_digest"],
            "status": "UNINSTALLED" if row["skill_status"] == "UNINSTALLED" else row["status"],
        }

    def effective_tools(
        self, version_id: str, *, global_tools: set[str] | None,
        role_tools: set[str] | None, phase_tools: set[str] | None,
    ) -> set[str]:
        if global_tools is None or role_tools is None or phase_tools is None:
            return set()
        try:
            item = self.version(version_id)
        except (KeyError, ValueError, json.JSONDecodeError):
            return set()
        if item["status"] != "ENABLED" or not item.get("grant_digest"):
            return set()
        return set(global_tools) & set(item["requested_tools"]) & set(item["granted_tools"]) & set(role_tools) & set(phase_tools)

    def tool_authorization(
        self,
        binding_type: str,
        binding_id: str,
        tool_name: str,
        *,
        connector_version_id: str | None = None,
        global_tools: set[str] | None,
        role_tools: set[str] | None,
        phase_tools: set[str] | None,
        routing_policy_digest: str,
    ) -> dict[str, str] | None:
        try:
            binding = self.binding(binding_type, binding_id)
        except KeyError:
            return None
        for version_id in binding["version_ids"]:
            if tool_name not in self.effective_tools(
                version_id, global_tools=global_tools, role_tools=role_tools, phase_tools=phase_tools,
            ):
                continue
            item = self.version(version_id)
            if connector_version_id:
                try:
                    with self.db.connection() as connection:
                        connector = connection.execute(
                            "SELECT c.name FROM trusted_connector_versions v JOIN trusted_connectors c ON c.id=v.connector_id "
                            "WHERE v.id=? AND c.owner_id=? AND c.status='ENABLED'",
                            (connector_version_id, self.owner_id),
                        ).fetchone()
                except (ValueError, json.JSONDecodeError):
                    return None
                if connector is None or connector["name"] not in item["connectors"]:
                    continue
            return {
                "skill_version_id": version_id,
                "package_digest": item["package_digest"],
                "grant_snapshot_digest": item["grant_digest"],
                "routing_policy_digest": routing_policy_digest,
                "binding_snapshot_digest": binding["snapshot_digest"],
            }
        return None

    def bind(self, binding_type: str, binding_id: str, version_ids: list[str], *, idempotency_key: str) -> dict[str, Any]:
        if binding_type not in {"THREAD", "RUN"} or not binding_id or not isinstance(version_ids, list):
            raise SkillValidationError("invalid skill binding")
        unique = list(dict.fromkeys(version_ids))
        now = _now()
        snapshot = []
        for version_id in unique:
            item = self.version(version_id)
            if item["status"] != "ENABLED":
                raise SkillValidationError("skill version is not enabled")
            snapshot.append({"version_id": version_id, "package_digest": item["package_digest"], "grant_digest": item["grant_digest"]})
        digest = _digest(snapshot)
        with self.db.transaction() as connection:
            cached = connection.execute("SELECT * FROM skill_bindings WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if cached:
                return self._binding(cached)
            prior = connection.execute(
                "SELECT * FROM skill_bindings WHERE owner_id=? AND binding_type=? AND binding_id=?",
                (self.owner_id, binding_type, binding_id),
            ).fetchone()
            if prior:
                raise SkillValidationError("binding is already frozen")
            binding_id_value = f"skill_binding_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO skill_bindings(id,owner_id,binding_type,binding_id,version_ids_json,snapshot_digest,idempotency_key,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (binding_id_value, self.owner_id, binding_type, binding_id, _json(unique), digest, idempotency_key, now, now),
            )
            return self._binding(connection.execute("SELECT * FROM skill_bindings WHERE id=?", (binding_id_value,)).fetchone())

    def binding(self, binding_type: str, binding_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM skill_bindings WHERE owner_id=? AND binding_type=? AND binding_id=?",
                (self.owner_id, binding_type, binding_id),
            ).fetchone()
        if row is None:
            raise KeyError(binding_id)
        return self._binding(row)

    def uninstall(self, skill_id: str, *, idempotency_key: str) -> None:
        with self.db.transaction() as connection:
            cached = connection.execute("SELECT 1 FROM skill_events WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if cached: return
            skill = connection.execute("SELECT * FROM skills WHERE id=? AND owner_id=?", (skill_id, self.owner_id)).fetchone()
            if skill is None: raise KeyError(skill_id)
            now = _now()
            connection.execute("UPDATE skills SET status='UNINSTALLED',default_version_id=NULL,updated_at=?,uninstalled_at=? WHERE id=?", (now, now, skill_id))
            connection.execute("UPDATE skill_versions SET status='UNINSTALLED' WHERE skill_id=?", (skill_id,))
            connection.execute("UPDATE skill_grants SET status='REVOKED',version=version+1,updated_at=? WHERE skill_version_id IN (SELECT id FROM skill_versions WHERE skill_id=?)", (now, skill_id))
            self._event(connection, skill_id, None, "skill.uninstalled", {}, idempotency_key)

    def _validate_package(self, package: bytes) -> _Preview:
        if not isinstance(package, bytes) or not package or len(package) > MAX_ZIP_BYTES:
            raise SkillValidationError("skill ZIP size is invalid")
        try:
            archive = zipfile.ZipFile(io.BytesIO(package))
        except zipfile.BadZipFile as exc:
            raise SkillValidationError("invalid skill ZIP") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_FILES:
                raise SkillValidationError("skill ZIP has too many files")
            total, normalized = 0, set()
            for info in infos:
                if info.flag_bits & 0x1:
                    raise SkillValidationError("encrypted skill ZIP is not allowed")
                path = _safe_archive_path(info.filename)
                key = path.as_posix().casefold()
                if key in normalized:
                    raise SkillValidationError("duplicate normalized path")
                normalized.add(key)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise SkillValidationError("symbolic link path is not allowed")
                if info.file_size > MAX_FILE_BYTES:
                    raise SkillValidationError("skill file size exceeds limit")
                total += info.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise SkillValidationError("skill unpacked size exceeds limit")
                name = path.as_posix()
                if not (name in {"skill.json", "SKILL.md"} or name.startswith("assets/") or name.startswith("evals/")):
                    raise SkillValidationError("unsupported skill package path")
            if "skill.json" not in normalized or "skill.md" not in normalized:
                raise SkillValidationError("skill.json and SKILL.md are required")
            document_bytes = archive.read("SKILL.md")
            if len(document_bytes) > MAX_SKILL_DOCUMENT_BYTES:
                raise SkillValidationError("SKILL.md exceeds size limit")
            try:
                manifest = json.loads(archive.read("skill.json").decode("utf-8"))
                content = document_bytes.decode("utf-8").strip()
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SkillValidationError("skill manifest and document must be valid UTF-8") from exc
        manifest = _validate_manifest(manifest)
        return _Preview(package, manifest, _digest(manifest), hashlib.sha256(package).hexdigest(), content)

    @staticmethod
    def _extract(package: bytes, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            for info in archive.infolist():
                path = _safe_archive_path(info.filename)
                if info.is_dir(): continue
                destination = target.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(info))

    @staticmethod
    def _binding(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"], "binding_type": row["binding_type"], "binding_id": row["binding_id"],
            "version_ids": json.loads(row["version_ids_json"]), "snapshot_digest": row["snapshot_digest"],
        }

    def _event(self, connection: Any, skill_id: str | None, version_id: str | None, event_type: str, data: dict[str, Any], key: str) -> None:
        connection.execute(
            "INSERT INTO skill_events(event_id,owner_id,skill_id,skill_version_id,type,actor,data_json,idempotency_key,occurred_at) "
            "VALUES (?,?,?,?,?,'user',?,?,?)",
            (f"skill_event_{uuid.uuid4().hex}", self.owner_id, skill_id, version_id, event_type, _json(data), key, _now()),
        )


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SkillValidationError("unsupported skill manifest schema")
    required_strings = ("name", "version", "title", "description", "entry_document")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required_strings):
        raise SkillValidationError("skill manifest fields are invalid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value["name"]):
        raise SkillValidationError("skill name is invalid")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?", value["version"]):
        raise SkillValidationError("skill version is invalid")
    if value["entry_document"] != "SKILL.md":
        raise SkillValidationError("entry document must be SKILL.md")
    for key in ("requested_tools", "connectors", "phases"):
        if not isinstance(value.get(key), list) or any(not isinstance(item, str) for item in value[key]):
            raise SkillValidationError(f"{key} must be a string array")
        value[key] = list(dict.fromkeys(value[key]))
    if any(phase not in ALLOWED_PHASES for phase in value["phases"]):
        raise SkillValidationError("skill phase is invalid")
    return {key: value[key] for key in (*required_strings, "schema_version", "requested_tools", "connectors", "phases")}


def _safe_archive_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts) or ":" in path.parts[0]:
        raise SkillValidationError("skill package path is unsafe")
    return path


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
