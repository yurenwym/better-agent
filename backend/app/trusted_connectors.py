from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, unquote, urlencode, urlsplit

from .db import Database


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class ConnectorSecurityError(ValueError):
    pass


class ConnectorReconciliationRequired(RuntimeError):
    pass


Resolver = Callable[[str], list[str]]
Requester = Callable[
    [str, str, str, str, dict[str, str], bytes | None, float, int],
    tuple[int, dict[str, str], bytes, str],
]


class TrustedConnectorService:
    def __init__(
        self,
        db: Database,
        *,
        owner_id: str = "local-user",
        resolver: Resolver | None = None,
        requester: Requester | None = None,
    ) -> None:
        self.db = db
        self.owner_id = owner_id
        self.resolver = resolver or _resolve
        self.requester = requester or _request

    def register(
        self,
        name: str,
        base_url: str,
        methods: list[str],
        paths: list[str],
        credential_env_ref: str | None,
        *,
        idempotency_key: str,
        timeout_seconds: float = 30,
        max_response_bytes: int = 2 * 1024 * 1024,
        request_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        host = _validate_base_url(base_url)
        del host
        normalized_methods = sorted(set(method.upper() for method in methods))
        if not normalized_methods or any(method not in ALLOWED_METHODS for method in normalized_methods):
            raise ConnectorSecurityError("connector method is not allowed")
        normalized_paths = sorted(set(paths))
        if not normalized_paths or any(not _safe_path(path) for path in normalized_paths):
            raise ConnectorSecurityError("connector path is not allowed")
        if credential_env_ref is not None and (
            not credential_env_ref or not credential_env_ref.replace("_", "A").isalnum()
        ):
            raise ConnectorSecurityError("credential must be an environment variable reference")
        if not name.strip() or timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ConnectorSecurityError("invalid connector configuration")
        config = {
            "base_url": base_url,
            "methods": normalized_methods,
            "paths": normalized_paths,
            "credential_env_ref": credential_env_ref,
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
            "request_schema": request_schema or {},
        }
        config_digest = _digest(config)
        request_digest = _digest({"name": name, **config})
        now = _now()
        with self.db.transaction() as connection:
            cached = connection.execute(
                "SELECT data_json FROM skill_events WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if cached:
                data = json.loads(cached["data_json"])
                if data.get("request_digest") != request_digest:
                    raise ConnectorSecurityError("idempotency key binding changed")
                return self.version(data["version_id"], connection=connection)
            connector = connection.execute(
                "SELECT * FROM trusted_connectors WHERE owner_id=? AND name=?", (self.owner_id, name)
            ).fetchone()
            connector_id = connector["id"] if connector else f"connector_{uuid.uuid4().hex}"
            if connector is None:
                connection.execute(
                    "INSERT INTO trusted_connectors(id,owner_id,name,status,created_at,updated_at) "
                    "VALUES (?,?,?,'ENABLED',?,?)",
                    (connector_id, self.owner_id, name, now, now),
                )
                version = 1
            else:
                prior = connection.execute(
                    "SELECT id FROM trusted_connector_versions WHERE connector_id=? AND config_digest=?",
                    (connector_id, config_digest),
                ).fetchone()
                if prior:
                    version_id = prior["id"]
                    connection.execute(
                        "UPDATE trusted_connectors SET status='ENABLED',current_version_id=?,updated_at=? WHERE id=?",
                        (version_id, now, connector_id),
                    )
                    self._event(connection, connector_id, version_id, request_digest, idempotency_key, now)
                    return self.version(version_id, connection=connection)
                version = connection.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM trusted_connector_versions WHERE connector_id=?",
                    (connector_id,),
                ).fetchone()[0]
            version_id = f"connector_version_{uuid.uuid4().hex}"
            risk = "WRITE" if any(method in WRITE_METHODS for method in normalized_methods) else "READ"
            connection.execute(
                "INSERT INTO trusted_connector_versions(id,connector_id,version,base_url,methods_json,paths_json,"
                "credential_env_ref,request_schema_json,timeout_seconds,max_response_bytes,risk,config_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id, connector_id, version, base_url, _json(normalized_methods), _json(normalized_paths),
                    credential_env_ref, _json(request_schema or {}), timeout_seconds, max_response_bytes, risk, config_digest, now,
                ),
            )
            connection.execute(
                "UPDATE trusted_connectors SET status='ENABLED',current_version_id=?,updated_at=? WHERE id=?",
                (version_id, now, connector_id),
            )
            self._event(connection, connector_id, version_id, request_digest, idempotency_key, now)
            return self.version(version_id, connection=connection)

    def version(self, version_id: str, *, connection=None) -> dict[str, Any]:
        owns_connection = connection is None
        if owns_connection:
            context = self.db.connection()
            connection = context.__enter__()
        try:
            row = connection.execute(
                "SELECT v.*,c.name,c.status AS connector_status FROM trusted_connector_versions v "
                "JOIN trusted_connectors c ON c.id=v.connector_id "
                "WHERE v.id=? AND c.owner_id=?",
                (version_id, self.owner_id),
            ).fetchone()
            if row is None:
                raise KeyError(version_id)
            return {
                "connector_id": row["connector_id"], "version_id": row["id"], "name": row["name"],
                "version": row["version"], "base_url": row["base_url"],
                "methods": json.loads(row["methods_json"]), "paths": json.loads(row["paths_json"]),
                "request_schema": json.loads(row["request_schema_json"]),
                "credential_env_ref": row["credential_env_ref"], "timeout_seconds": row["timeout_seconds"],
                "max_response_bytes": row["max_response_bytes"], "risk": row["risk"],
                "config_digest": row["config_digest"], "status": row["connector_status"],
            }
        finally:
            if owns_connection:
                context.__exit__(None, None, None)

    def list(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT v.id FROM trusted_connector_versions v JOIN trusted_connectors c ON c.id=v.connector_id "
                "WHERE c.owner_id=? ORDER BY c.name,v.version DESC", (self.owner_id,),
            ).fetchall()
        return [self.version(row["id"]) for row in rows]

    def verify(self, version_id: str) -> dict[str, Any]:
        item = self.version(version_id)
        host = urlsplit(item["base_url"]).hostname or ""
        addresses = list(dict.fromkeys(self.resolver(host)))
        if not addresses or len(addresses) > 16 or any(not _public_ip(address) for address in addresses):
            raise ConnectorSecurityError("connector DNS must resolve only to public addresses")
        now = _now()
        with self.db.transaction() as connection:
            connection.execute("UPDATE trusted_connector_versions SET verified_at=? WHERE id=?", (now, version_id))
        return {**self.version(version_id), "verified_at": now, "verified_addresses": len(addresses)}

    def execute(
        self,
        version_id: str,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        item = self.version(version_id)
        if item["status"] != "ENABLED":
            raise ConnectorSecurityError("connector is disabled")
        method = method.upper()
        if method not in item["methods"]:
            raise ConnectorSecurityError("connector method is not allowed")
        if not _safe_path(path) or path not in item["paths"]:
            raise ConnectorSecurityError("connector path is not allowed")
        split = urlsplit(item["base_url"])
        addresses = list(dict.fromkeys(self.resolver(split.hostname or "")))
        if not addresses or any(not _public_ip(address) for address in addresses):
            raise ConnectorSecurityError("connector DNS must resolve only to public addresses")
        limit = min(max_response_bytes or item["max_response_bytes"], item["max_response_bytes"])
        request_path = path
        if query:
            request_path += "?" + urlencode(query, doseq=True)
        payload = _json(body).encode("utf-8") if body is not None else None
        headers = {"accept": "application/json"}
        if payload is not None:
            headers["content-type"] = "application/json"
        if item["credential_env_ref"]:
            secret = os.environ.get(item["credential_env_ref"])
            if secret is None:
                raise ConnectorSecurityError("connector credential environment variable is missing")
            headers["authorization"] = f"Bearer {secret}"
        try:
            status, response_headers, raw, peer_ip = self.requester(
                split.hostname or "", addresses[0], method, request_path, headers, payload,
                item["timeout_seconds"], limit,
            )
        except OSError as exc:
            if method in WRITE_METHODS:
                raise ConnectorReconciliationRequired("connector write result is unknown") from exc
            raise ConnectorSecurityError("connector request failed") from exc
        if peer_ip not in addresses or not _public_ip(peer_ip):
            raise ConnectorSecurityError("connector peer IP did not match validated DNS")
        if 300 <= status < 400:
            raise ConnectorSecurityError("connector redirect is not allowed")
        if len(raw) > limit:
            raise ConnectorSecurityError("connector response exceeds size limit")
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectorSecurityError("connector response must be valid UTF-8 JSON") from exc
        return {
            "status": status,
            "body": parsed,
            "content_type": response_headers.get("content-type", ""),
            "untrusted_external_data": True,
        }

    def _event(self, connection, connector_id: str, version_id: str, request_digest: str, key: str, now: str) -> None:
        connection.execute(
            "INSERT INTO skill_events(event_id,owner_id,type,actor,data_json,idempotency_key,occurred_at) "
            "VALUES (?,?, 'connector.registered','user',?,?,?)",
            (f"skill_event_{uuid.uuid4().hex}", self.owner_id, _json({
                "connector_id": connector_id, "version_id": version_id, "request_digest": request_digest,
            }), key, now),
        )


def _validate_base_url(value: str) -> str:
    split = urlsplit(value)
    if (
        split.scheme != "https" or not split.hostname or split.username is not None or split.password is not None
        or split.port not in (None, 443) or split.path not in ("", "/") or split.query or split.fragment
    ):
        raise ConnectorSecurityError("connector base URL must be HTTPS on port 443 without path or user info")
    try:
        ipaddress.ip_address(split.hostname)
    except ValueError:
        pass
    else:
        raise ConnectorSecurityError("connector base URL must use a DNS hostname")
    if split.hostname.lower() == "localhost" or "." not in split.hostname:
        raise ConnectorSecurityError("connector base URL hostname is not allowed")
    return split.hostname


def _safe_path(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("/") or "?" in value or "#" in value:
        return False
    decoded = unquote(value)
    return all(part not in {".", ".."} for part in decoded.split("/"))


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_global
    return address.is_global


def _resolve(host: str) -> list[str]:
    return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))


def _request(
    host: str,
    ip: str,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    max_bytes: int,
) -> tuple[int, dict[str, str], bytes, str]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    raw_socket = socket.socket(family, socket.SOCK_STREAM)
    raw_socket.settimeout(timeout)
    try:
        raw_socket.connect((ip, 443))
        tls = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
        request_headers = {"host": host, "connection": "close", "content-length": str(len(body or b"")), **headers}
        request = f"{method} {quote(path, safe='/?=&+%:,')} HTTP/1.1\r\n".encode("ascii")
        request += b"".join(f"{key}: {value}\r\n".encode("utf-8") for key, value in request_headers.items()) + b"\r\n"
        tls.sendall(request + (body or b""))
        response = http.client.HTTPResponse(tls)
        response.begin()
        payload = response.read(max_bytes + 1)
        peer = tls.getpeername()[0]
        return response.status, {key.lower(): value for key, value in response.getheaders()}, payload, peer
    finally:
        raw_socket.close()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
