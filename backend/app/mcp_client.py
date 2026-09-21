"""MCP client manager: configuration, connection lifecycle, discovery cache, calls.

The Harness keeps Tool Registry, permissions, approvals, budget, context,
recovery and observability. MCP only standardises *how an external capability is
reached*, so this module deliberately owns a narrow slice:

* resolve the local server configuration (including credential indirection),
* run one long-lived client session per enabled server,
* perform capability and tool discovery, with the protocol's cache hints,
* expose the discovered catalogue synchronously for schema projection,
* call a remote tool with an explicit deadline,
* release client resources on shutdown.

Nothing here decides whether a tool may run. That stays in
:class:`app.tools.ToolRegistry` and :class:`app.chat_tools.ChatToolRunner`.

Credential values never leave this module: they are read from the environment
into the transport parameters and only a one-way digest of them is folded into
the public ``config_version``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

MCP_SERVERS_ENV = "BETTER_AGENT_MCP_SERVERS"

# One page of discovery should never be allowed to fan out without bound.
MAX_DISCOVERY_PAGES = 32
# Outer deadline margin so the SDK's own read timeout classifies the failure
# before the registry's guard cancels the coroutine mid-flight.
_CALL_DEADLINE_RATIO = 0.9
_CONNECT_TIMEOUT_SECONDS = 20.0

STDIO_TRANSPORT = "stdio"
HTTP_TRANSPORT = "http"

_RISK_VALUES = {"PURE", "READ", "WRITE"}


class McpError(RuntimeError):
    """Base class for every MCP failure the Harness reports as a tool error."""


class McpConfigError(McpError):
    """The local server configuration is unusable."""


class McpUnavailable(McpError):
    """The server could not be reached or never completed its handshake."""


class McpDisconnected(McpError):
    """The connection was lost; the remote side may or may not have acted."""


class McpProtocolError(McpError):
    """The server answered with something the client cannot interpret."""


@dataclass(frozen=True)
class McpServerConfig:
    """One configured server. Credentials live only in ``env``/``headers``."""

    server_id: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    # A locally launched server normally needs PATH, proxies and locale. Set
    # this to false to give the child only the SDK's minimal environment plus
    # the explicitly configured entries.
    inherit_env: bool = True
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # ``None`` means "every tool the server advertises".
    enabled_tools: tuple[str, ...] | None = None
    # Local risk policy: remote tool name -> PURE/READ/WRITE.
    risk: dict[str, str] = field(default_factory=dict)
    default_risk: str = "READ"
    # Phase 1 posture: a server that is not explicitly trusted for side effects
    # must not expose write tools at all.
    allow_write_tools: bool = False
    timeout_seconds: float = 30.0
    max_result_bytes: int = 65536
    # Operator override for servers that predate the protocol's cache hints.
    # ``None`` keeps the spec behaviour: no hint means immediately stale.
    cache_ttl_seconds: float | None = None
    protocol_version: str | None = None
    # remote write tool -> read-only receipt tool used for reconciliation.
    receipt_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Resource scope, resolved locally from the validated parameters: remote
    # tool -> parameter name -> the values the operator authorised. A model may
    # ask for another repository; the configuration decides whether it exists.
    resource_allow: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    config_version: str = ""

    def public_view(self) -> dict[str, Any]:
        """Configuration without credential values, safe for events and logs.

        Credentials are supposed to arrive through ``env``/``headers``
        indirection, but the two other places an operator can accidentally put
        one - a URL query string or a ``--token=`` argument - are also scrubbed
        here. This view goes into events and logs, so "safe for logs" has to
        hold even when the configuration is not what the documentation advises.
        """
        return {
            "server_id": self.server_id,
            "transport": self.transport,
            "command": self.command,
            "args": redact_args(self.args),
            "inherit_env": self.inherit_env,
            "url": redact_url(self.url),
            "env_names": sorted(self.env),
            "header_names": sorted(self.headers),
            "enabled_tools": list(self.enabled_tools) if self.enabled_tools is not None else None,
            "risk": dict(self.risk),
            "default_risk": self.default_risk,
            "allow_write_tools": self.allow_write_tools,
            "timeout_seconds": self.timeout_seconds,
            "max_result_bytes": self.max_result_bytes,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "protocol_version": self.protocol_version,
            "resource_allow": {
                tool: {name: list(values) for name, values in params.items()}
                for tool, params in self.resource_allow.items()
            },
            "config_version": self.config_version,
        }

    def risk_for(self, remote_name: str) -> str:
        configured = self.risk.get(remote_name)
        if configured is not None:
            return configured
        return self.default_risk

    @property
    def target_hint(self) -> str:
        """Where this server was supposed to be reached, safe to log.

        A failed spawn otherwise reports only the SDK's innermost error, which
        for a process that exits immediately is the useless "Connection
        closed". Naming the command or endpoint turns that into something an
        operator can act on.
        """
        if self.transport == STDIO_TRANSPORT:
            return " ".join([self.command or "", *redact_args(self.args)]).strip()
        return redact_url(self.url) or ""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Flag names that mark the following value as a credential rather than data.
_SENSITIVE_FLAG_MARKERS = (
    "token", "key", "secret", "password", "passwd", "credential", "auth", "cookie",
)


def redact_url(url: str | None) -> str | None:
    """Strip userinfo, query and fragment from a URL before logging it.

    ``https://user:token@host/mcp?api_key=...`` is a common way to configure a
    remote server, and every one of those parts ends up in events. The host and
    path are what an operator needs to see; the rest is credential material.
    """
    if not url:
        return url
    parsed = urlsplit(url)
    if not parsed.netloc:
        return url
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _is_sensitive_flag(flag: str) -> bool:
    lowered = flag.lstrip("-").lower()
    return any(marker in lowered for marker in _SENSITIVE_FLAG_MARKERS)


def redact_args(args: Iterable[str]) -> list[str]:
    """Mask values of command-line flags whose name looks credential-bearing.

    A credential belongs in ``env`` with a ``from_env`` reference. When one is
    passed as ``--api-key=...`` instead, the value must not reach an event body
    or a log line. Only the value is replaced, so the command stays diagnosable.
    """
    redacted: list[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            redacted.append("<redacted>")
            mask_next = False
            continue
        if arg.startswith("-") and "=" in arg:
            flag, _, _value = arg.partition("=")
            redacted.append(f"{flag}=<redacted>" if _is_sensitive_flag(flag) else arg)
            continue
        if arg.startswith("-") and _is_sensitive_flag(arg):
            redacted.append(arg)
            mask_next = True
            continue
        redacted.append(arg)
    return redacted


def _credential_digest(env: dict[str, str], headers: dict[str, str]) -> str:
    """One-way digest of secret *values*, so a rotation invalidates the cache."""
    material = _canonical({"env": sorted(env.items()), "headers": sorted(headers.items())})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _config_version(payload: dict[str, Any], env: dict[str, str], headers: dict[str, str]) -> str:
    material = _canonical(payload) + "|" + _credential_digest(env, headers)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _resolve_env(mapping: Any, *, field_name: str, server_id: str) -> dict[str, str]:
    """``{"NAME": {"from_env": "VAR"}}`` or ``{"NAME": "literal"}``."""
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise McpConfigError(f"{server_id}: {field_name} must be an object")
    resolved: dict[str, str] = {}
    for key, value in mapping.items():
        if isinstance(value, dict):
            source = value.get("from_env")
            if not isinstance(source, str) or not source:
                raise McpConfigError(f"{server_id}: {field_name}.{key} needs from_env")
            if value.get("required", True) and not os.getenv(source):
                raise McpConfigError(f"{server_id}: environment variable {source} is not set")
            resolved[str(key)] = os.getenv(source, "")
        elif isinstance(value, str):
            resolved[str(key)] = value
        else:
            raise McpConfigError(f"{server_id}: {field_name}.{key} must be a string or {{from_env}}")
    return resolved


@dataclass(frozen=True)
class ConfigRejection:
    """One server entry that could not be used, and why.

    Kept as data rather than an exception so a typo in one entry degrades that
    entry only: the other configured servers still register their tools.
    """

    index: int
    server_id: str | None
    reason: str


def _decode_entries(raw: str | None) -> list[Any]:
    """The JSON document itself. A broken document cannot be recovered per entry."""
    if raw is None or not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise McpConfigError("BETTER_AGENT_MCP_SERVERS is not valid JSON") from exc
    if not isinstance(payload, list):
        raise McpConfigError("BETTER_AGENT_MCP_SERVERS must be a JSON array")
    return payload


def parse_server_configs_lenient(
    raw: str | None,
) -> tuple[list[McpServerConfig], list[ConfigRejection]]:
    """Parse the JSON array entry by entry, dropping only the broken entries.

    Startup must not be blocked by an optional integration, but "one bad entry
    disables every server" is not acceptable either: a rejected entry is
    reported with its index so the operator can see exactly what was dropped.
    A dropped entry never half-registers - fail-closed means its tools simply
    do not exist.
    """
    try:
        payload = _decode_entries(raw)
    except McpConfigError as exc:
        return [], [ConfigRejection(index=0, server_id=None, reason=str(exc))]

    configs: list[McpServerConfig] = []
    rejections: list[ConfigRejection] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        label = item.get("server_id") if isinstance(item, dict) else None
        label = label if isinstance(label, str) else None
        try:
            config = server_config_from_dict(item)
        except McpConfigError as exc:
            rejections.append(ConfigRejection(index=index, server_id=label, reason=str(exc)))
            continue
        if config.server_id in seen:
            rejections.append(ConfigRejection(
                index=index, server_id=config.server_id,
                reason=f"duplicate MCP server_id: {config.server_id}",
            ))
            continue
        seen.add(config.server_id)
        configs.append(config)
    return configs, rejections


def parse_server_configs(raw: str | None) -> list[McpServerConfig]:
    """Parse the ``BETTER_AGENT_MCP_SERVERS`` JSON array, strictly.

    An unset or empty value means "no MCP servers", which must not be an error:
    a single optional server being unavailable never blocks plain chat.
    The first unusable entry raises, which is the right behaviour for callers
    that validate an operator-supplied document rather than boot the app.
    """
    configs, rejections = parse_server_configs_lenient(raw)
    if rejections:
        raise McpConfigError(rejections[0].reason)
    return configs


def server_config_from_dict(item: Any) -> McpServerConfig:
    if not isinstance(item, dict):
        raise McpConfigError("each MCP server entry must be an object")
    server_id = item.get("server_id")
    if not isinstance(server_id, str) or not server_id.strip():
        raise McpConfigError("MCP server entry needs a non-empty server_id")
    transport = str(item.get("transport", STDIO_TRANSPORT)).strip().lower()
    if transport not in {STDIO_TRANSPORT, HTTP_TRANSPORT}:
        raise McpConfigError(f"{server_id}: unsupported transport {transport!r}")
    env = _resolve_env(item.get("env"), field_name="env", server_id=server_id)
    headers = _resolve_env(item.get("headers"), field_name="headers", server_id=server_id)
    enabled = item.get("enabled_tools")
    if enabled is not None:
        if not isinstance(enabled, list) or not all(isinstance(name, str) and name for name in enabled):
            raise McpConfigError(f"{server_id}: enabled_tools must be a list of names")
        enabled = tuple(enabled)
    risk = item.get("risk") or {}
    if not isinstance(risk, dict) or not all(str(value).upper() in _RISK_VALUES for value in risk.values()):
        raise McpConfigError(f"{server_id}: risk must map tool names to PURE/READ/WRITE")
    default_risk = str(item.get("default_risk", "READ")).upper()
    if default_risk not in _RISK_VALUES:
        raise McpConfigError(f"{server_id}: default_risk must be PURE/READ/WRITE")
    command = item.get("command")
    url = item.get("url")
    if transport == STDIO_TRANSPORT and not command:
        raise McpConfigError(f"{server_id}: stdio transport needs a command")
    if transport == HTTP_TRANSPORT and not url:
        raise McpConfigError(f"{server_id}: http transport needs a url")
    args = item.get("args") or []
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise McpConfigError(f"{server_id}: args must be a list of strings")
    receipt_tools = item.get("receipt_tools") or {}
    if not isinstance(receipt_tools, dict):
        raise McpConfigError(f"{server_id}: receipt_tools must be an object")
    resource_allow: dict[str, dict[str, tuple[str, ...]]] = {}
    raw_resource_allow = item.get("resource_allow") or {}
    if not isinstance(raw_resource_allow, dict):
        raise McpConfigError(f"{server_id}: resource_allow must be an object")
    for tool_name, constraints in raw_resource_allow.items():
        if not isinstance(constraints, dict):
            raise McpConfigError(f"{server_id}: resource_allow.{tool_name} must be an object")
        resolved_constraints: dict[str, tuple[str, ...]] = {}
        for parameter, values in constraints.items():
            if not isinstance(values, list) or not values:
                raise McpConfigError(
                    f"{server_id}: resource_allow.{tool_name}.{parameter} must be a non-empty list"
                )
            resolved_constraints[str(parameter)] = tuple(str(value) for value in values)
        resource_allow[str(tool_name)] = resolved_constraints
    cache_ttl = item.get("cache_ttl_seconds")
    if cache_ttl is not None:
        try:
            cache_ttl = float(cache_ttl)
        except (TypeError, ValueError) as exc:
            raise McpConfigError(f"{server_id}: cache_ttl_seconds must be a number") from exc
        if cache_ttl <= 0:
            cache_ttl = None
    timeout_seconds = float(item.get("timeout_seconds", 30.0))
    if timeout_seconds <= 0:
        raise McpConfigError(f"{server_id}: timeout_seconds must be positive")
    max_result_bytes = int(item.get("max_result_bytes", 65536))
    if max_result_bytes <= 0:
        raise McpConfigError(f"{server_id}: max_result_bytes must be positive")
    payload = {
        "server_id": server_id,
        "transport": transport,
        "command": command,
        "args": args,
        "cwd": item.get("cwd"),
        "inherit_env": bool(item.get("inherit_env", True)),
        "url": url,
        "enabled_tools": sorted(enabled) if enabled is not None else None,
        "risk": {str(name): str(value).upper() for name, value in risk.items()},
        "default_risk": default_risk,
        "allow_write_tools": bool(item.get("allow_write_tools", False)),
        "timeout_seconds": timeout_seconds,
        "max_result_bytes": max_result_bytes,
        "cache_ttl_seconds": cache_ttl,
        "protocol_version": item.get("protocol_version"),
        "receipt_tools": receipt_tools,
        "resource_allow": resource_allow,
    }
    return McpServerConfig(
        **payload,
        env=env,
        headers=headers,
        config_version=_config_version(
            {**payload, "env_names": sorted(env), "header_names": sorted(headers)}, env, headers,
        ),
    )


@dataclass(frozen=True)
class RemoteTool:
    """One tool definition as advertised by a server, with a local digest."""

    name: str
    title: str | None
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any]
    meta: dict[str, Any]
    definition_digest: str

    @property
    def read_only_hint(self) -> bool | None:
        value = self.annotations.get("readOnlyHint")
        return value if isinstance(value, bool) else None

    @property
    def task_support(self) -> str | None:
        return self.meta.get("taskSupport")

    def definition_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": self.annotations,
        }


def _tool_definition_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _to_remote_tool(tool: Any) -> RemoteTool:
    definition = tool.model_dump(by_alias=True, exclude_none=True)
    meta = dict(definition.get("_meta") or definition.get("meta") or {})
    execution = definition.get("execution") or {}
    if isinstance(execution, dict) and execution.get("taskSupport"):
        meta["taskSupport"] = execution["taskSupport"]
    annotations = dict(definition.get("annotations") or {})
    input_schema = definition.get("inputSchema")
    if not isinstance(input_schema, dict):
        raise McpProtocolError(f"tool {definition.get('name')!r} has no object input schema")
    public_definition = {
        "name": definition.get("name"),
        "title": definition.get("title"),
        "description": definition.get("description"),
        "inputSchema": input_schema,
        "outputSchema": definition.get("outputSchema"),
        "annotations": annotations,
    }
    return RemoteTool(
        name=str(definition.get("name") or ""),
        title=definition.get("title"),
        description=str(definition.get("description") or ""),
        input_schema=input_schema,
        output_schema=definition.get("outputSchema"),
        annotations=annotations,
        meta=meta,
        definition_digest=_tool_definition_digest(public_definition),
    )


def ttl_seconds_from_meta(meta: dict[str, Any] | None) -> float | None:
    """Protocol cache hint. ``None`` means "treat as immediately stale"."""
    if not isinstance(meta, dict):
        return None
    raw = meta.get("ttlMs")
    if raw is None:
        return None
    try:
        milliseconds = float(raw)
    except (TypeError, ValueError):
        return None
    if milliseconds <= 0:
        return None
    return milliseconds / 1000.0


def cache_scope_from_meta(meta: dict[str, Any] | None) -> str:
    if isinstance(meta, dict) and meta.get("cacheScope") == "public":
        return "public"
    return "private"


@dataclass(frozen=True)
class CatalogPage:
    cursor: str | None
    next_cursor: str | None
    tools: tuple[RemoteTool, ...]
    fetched_at: float
    ttl_seconds: float | None

    def fresh(self, now: float) -> bool:
        if self.ttl_seconds is None:
            return False
        return now < self.fetched_at + self.ttl_seconds


@dataclass(frozen=True)
class CatalogSnapshot:
    """A complete, atomically replaced tool catalogue for one server+identity."""

    server_id: str
    config_version: str
    protocol_version: str
    auth_scope: str
    cache_scope: str
    pages: tuple[CatalogPage, ...]
    refreshed_at: float

    def tools(self) -> tuple[RemoteTool, ...]:
        return tuple(tool for page in self.pages for tool in page.tools)

    def tool(self, remote_name: str) -> RemoteTool | None:
        for tool in self.tools():
            if tool.name == remote_name:
                return tool
        return None

    def fresh(self, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        return all(page.fresh(moment) for page in self.pages)


@dataclass
class McpMetrics:
    """Countable evidence for the acceptance report."""

    discovery_requests: int = 0
    discovery_pages: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    catalog_refreshes: int = 0
    catalog_invalidations: int = 0
    cursor_restarts: int = 0
    connects: int = 0
    reconnects: int = 0
    call_requests: int = 0
    call_failures: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(vars(self))


class _SessionHandle:
    """Owns one server's client session and the task that keeps it alive."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.session: Any = None
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.last_error: str | None = None
        self.tools_changed = False
        self._task: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None

    @property
    def connected(self) -> bool:
        return self.session is not None

    @asynccontextmanager
    async def _transport(self):
        """Yield the transport's read/write streams, owning any extra resources."""
        if self.config.transport == STDIO_TRANSPORT:
            from mcp.client.stdio import StdioServerParameters, stdio_client

            if self.config.inherit_env or self.config.env:
                base = dict(os.environ) if self.config.inherit_env else {}
                child_env = {**base, **self.config.env}
            else:
                child_env = None
            parameters = StdioServerParameters(
                command=self.config.command or "",
                args=list(self.config.args),
                env=child_env,
                cwd=self.config.cwd,
            )
            async with stdio_client(parameters) as streams:
                yield streams
            return
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        client = httpx.AsyncClient(
            headers=self.config.headers or None,
            timeout=httpx.Timeout(self.config.timeout_seconds),
            follow_redirects=True,
        )
        try:
            async with streamable_http_client(
                self.config.url or "", http_client=client,
            ) as streams:
                yield streams
        finally:
            await client.aclose()

    async def _run(self) -> None:
        from mcp.client.session import ClientSession

        try:
            async with self._transport() as streams:
                read_stream, write_stream = streams[0], streams[1]
                # No session-wide read timeout: it would also bound the
                # handshake, and a short per-call timeout would then kill the
                # session before it finished initializing. Each request carries
                # its own deadline instead.
                async with ClientSession(
                    read_stream,
                    write_stream,
                    message_handler=self._on_message,
                ) as session:
                    result = await session.initialize()
                    self.session = session
                    self.protocol_version = result.protocolVersion
                    self.server_info = result.serverInfo.model_dump(by_alias=True, exclude_none=True)
                    self.capabilities = result.capabilities.model_dump(by_alias=True, exclude_none=True)
                    self.last_error = None
                    if self._ready is not None:
                        self._ready.set()
                    assert self._stop is not None
                    await self._stop.wait()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except BaseException as exc:  # noqa: BLE001 - reported as a tool error
            self.last_error = _describe_exception(exc)
            logger.warning("MCP server %s session ended: %s", self.config.server_id, self.last_error)
        finally:
            self.session = None
            if self._ready is not None:
                self._ready.set()

    async def _on_message(self, message: Any) -> None:
        method = getattr(message, "method", None) or getattr(message, "root", None)
        method = getattr(method, "method", method)
        if method == "notifications/tools/list_changed":
            self.tools_changed = True

    async def connect(self, *, timeout: float = _CONNECT_TIMEOUT_SECONDS) -> None:
        if self.connected:
            return
        if self._task is not None and not self._task.done():
            await self.close()
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self.last_error = None
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.config.server_id}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise McpUnavailable(
                f"MCP server {self.config.server_id} did not complete its handshake in {timeout:.0f}s"
            ) from exc
        if not self.connected:
            hint = self.config.target_hint
            where = f" ({hint})" if hint else ""
            raise McpUnavailable(
                f"MCP server {self.config.server_id} is unavailable{where}: "
                f"{self.last_error or 'no session'}"
            )

    async def close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        self.session = None

    def mark_broken(self, reason: str) -> None:
        """Record a connection the caller already proved unusable.

        The background task will notice the same failure on its own, but a call
        that just observed a drop must not leave a dead session looking usable:
        the next call has to reconnect rather than reuse it.
        """
        self.last_error = reason
        self.session = None

    def session_or_raise(self) -> Any:
        if self.session is None:
            raise McpDisconnected(
                f"MCP server {self.config.server_id} is not connected: {self.last_error or 'closed'}"
            )
        return self.session


class McpClientManager:
    """One client manager per process, owning sessions and discovery caches."""

    def __init__(
        self,
        configs: Iterable[McpServerConfig] | None = None,
        *,
        metrics: McpMetrics | None = None,
    ) -> None:
        self._configs: dict[str, McpServerConfig] = {}
        for config in configs or ():
            if config.server_id in self._configs:
                raise McpConfigError(f"duplicate MCP server_id: {config.server_id}")
            self._configs[config.server_id] = config
        self._handles: dict[str, _SessionHandle] = {}
        self._catalogs: dict[tuple[str, str], CatalogSnapshot] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.metrics = metrics or McpMetrics()
        # Observability sink: the Harness records discovery and cache decisions
        # so the acceptance report can show countable behaviour.
        self.event_sink = None
        # Entries dropped while reading the environment, kept for reporting.
        self.config_rejections: tuple[ConfigRejection, ...] = ()

    # ------------------------------------------------------------------ config

    @property
    def server_ids(self) -> tuple[str, ...]:
        return tuple(self._configs)

    def config(self, server_id: str) -> McpServerConfig:
        config = self._configs.get(server_id)
        if config is None:
            raise McpConfigError(f"unknown MCP server: {server_id}")
        return config

    def configs(self) -> tuple[McpServerConfig, ...]:
        return tuple(self._configs.values())

    # -------------------------------------------------------------- lifecycle

    async def start(self, server_ids: Iterable[str] | None = None) -> dict[str, str]:
        """Connect the requested servers. Returns ``server_id -> error``."""
        failures: dict[str, str] = {}
        for server_id in (server_ids if server_ids is not None else self.server_ids):
            config = self.config(server_id)
            handle = self._handles.setdefault(server_id, _SessionHandle(config))
            try:
                await handle.connect()
            except McpError as exc:
                failures[server_id] = str(exc)
                self._emit("mcp.server.unavailable", {"server_id": server_id, "reason": str(exc)})
                continue
            self.metrics.connects += 1
            self._emit(
                "mcp.server.connected",
                {
                    "server_id": server_id,
                    "protocol_version": handle.protocol_version,
                    "server_info": handle.server_info,
                    "config_version": config.config_version,
                },
            )
        return failures

    async def close(self) -> None:
        for server_id, handle in list(self._handles.items()):
            await handle.close()
            self._emit("mcp.server.closed", {"server_id": server_id})
        self._handles.clear()
        self._catalogs.clear()
        self._locks.clear()

    def handle(self, server_id: str) -> _SessionHandle:
        handle = self._handles.get(server_id)
        if handle is None:
            config = self.config(server_id)
            handle = _SessionHandle(config)
            self._handles[server_id] = handle
        return handle

    def connected(self, server_id: str) -> bool:
        handle = self._handles.get(server_id)
        return bool(handle is not None and handle.connected)

    def protocol_version(self, server_id: str) -> str | None:
        handle = self._handles.get(server_id)
        return handle.protocol_version if handle is not None else None

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(event_type, data)
        except Exception:  # noqa: BLE001 - observability must never break a call
            logger.debug("MCP event sink failed for %s", event_type, exc_info=True)

    # -------------------------------------------------------------- discovery

    def catalog(self, server_id: str, auth_scope: str) -> CatalogSnapshot | None:
        """Last known catalogue, without touching the network (sync callers)."""
        return self._catalogs.get((server_id, auth_scope))

    def peek_tools(self, server_id: str, auth_scope: str) -> tuple[RemoteTool, ...]:
        snapshot = self.catalog(server_id, auth_scope)
        return snapshot.tools() if snapshot is not None else ()

    def invalidate(self, server_id: str, auth_scope: str | None = None) -> None:
        """Drop the catalogue. Tool-change notifications land here."""
        keys = [
            key for key in self._catalogs
            if key[0] == server_id and (auth_scope is None or key[1] == auth_scope)
        ]
        for key in keys:
            self._catalogs.pop(key, None)
        if keys:
            self.metrics.catalog_invalidations += 1
            self._emit("mcp.catalog.invalidated", {"server_id": server_id, "auth_scope": auth_scope})

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def ensure_catalog(
        self,
        server_id: str,
        auth_scope: str,
        *,
        force: bool = False,
    ) -> CatalogSnapshot:
        """Return a fresh catalogue, refreshing it when the cache says stale."""
        key = (server_id, auth_scope)
        handle = self.handle(server_id)
        if handle.tools_changed:
            handle.tools_changed = False
            self.invalidate(server_id, auth_scope)
            force = True
        snapshot = self._catalogs.get(key)
        if snapshot is not None and snapshot.config_version != self.config(server_id).config_version:
            # Credentials or configuration changed: never reuse the old scope.
            self.invalidate(server_id, auth_scope)
            snapshot = None
        if not force and snapshot is not None and snapshot.fresh():
            self.metrics.cache_hits += 1
            self._emit(
                "mcp.catalog.cache_hit",
                {"server_id": server_id, "auth_scope": auth_scope, "tools": len(snapshot.tools())},
            )
            return snapshot
        self.metrics.cache_misses += 1
        async with self._lock_for(key):
            snapshot = self._catalogs.get(key)
            if not force and snapshot is not None and snapshot.fresh():
                self.metrics.cache_hits += 1
                return snapshot
            refreshed = await self._refresh(server_id, auth_scope)
            self._catalogs[key] = refreshed
            self.metrics.catalog_refreshes += 1
            self._emit(
                "mcp.catalog.refreshed",
                {
                    "server_id": server_id,
                    "auth_scope": auth_scope,
                    "tools": len(refreshed.tools()),
                    "pages": len(refreshed.pages),
                    "cache_scope": refreshed.cache_scope,
                    "ttl_seconds": [page.ttl_seconds for page in refreshed.pages],
                },
            )
            return refreshed

    async def _refresh(self, server_id: str, auth_scope: str) -> CatalogSnapshot:
        config = self.config(server_id)
        handle = self.handle(server_id)
        await self._ensure_session(handle)
        if "tools" not in handle.capabilities:
            raise McpProtocolError(f"MCP server {server_id} does not advertise tool support")
        # A cursor can expire between pages. When it does, the partial read is
        # discarded and the catalogue is read again from the first page rather
        # than merged into a half-updated registry.
        for attempt in (0, 1):
            try:
                return await self._read_all_pages(server_id, auth_scope, handle)
            except McpProtocolError:
                if attempt:
                    raise
                self.metrics.cursor_restarts += 1
                self._emit("mcp.catalog.cursor_restart", {"server_id": server_id})
        raise McpProtocolError(f"MCP server {server_id} discovery failed")  # pragma: no cover

    async def _read_all_pages(
        self, server_id: str, auth_scope: str, handle: _SessionHandle,
    ) -> CatalogSnapshot:
        config = self.config(server_id)
        pages: list[CatalogPage] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        cache_scope = "private"
        while True:
            self.metrics.discovery_requests += 1
            session = handle.session_or_raise()
            try:
                result = await asyncio.wait_for(
                    session.list_tools(cursor), timeout=config.timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise McpUnavailable(
                    f"MCP server {server_id} tools/list timed out after "
                    f"{config.timeout_seconds:.0f}s"
                ) from exc
            except Exception as exc:  # noqa: BLE001 - SDK error hierarchy is broad
                raise McpProtocolError(
                    f"MCP server {server_id} tools/list failed: {type(exc).__name__}: {exc}"
                ) from exc
            self.metrics.discovery_pages += 1
            meta = dict(getattr(result, "meta", None) or {})
            page_scope = cache_scope_from_meta(meta)
            cache_scope = page_scope if page_scope == "private" else cache_scope
            ttl = ttl_seconds_from_meta(meta)
            if ttl is None and config.cache_ttl_seconds is not None:
                # Operator override, used only for servers that predate the
                # protocol's cache hints. The spec default stays "stale now".
                ttl = config.cache_ttl_seconds
            try:
                tools = tuple(_to_remote_tool(tool) for tool in result.tools)
            except McpProtocolError:
                raise
            next_cursor = getattr(result, "nextCursor", None)
            pages.append(
                CatalogPage(
                    cursor=cursor,
                    next_cursor=next_cursor,
                    tools=tools,
                    fetched_at=time.monotonic(),
                    ttl_seconds=ttl,
                )
            )
            if next_cursor is None:
                break
            if next_cursor in seen_cursors:
                raise McpProtocolError(f"MCP server {server_id} returned a repeating cursor")
            if len(pages) >= MAX_DISCOVERY_PAGES:
                raise McpProtocolError(f"MCP server {server_id} exceeded {MAX_DISCOVERY_PAGES} discovery pages")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return CatalogSnapshot(
            server_id=server_id,
            config_version=config.config_version,
            protocol_version=handle.protocol_version or "unknown",
            auth_scope=auth_scope,
            cache_scope=cache_scope,
            pages=tuple(pages),
            refreshed_at=time.monotonic(),
        )

    async def _ensure_session(self, handle: _SessionHandle) -> None:
        if handle.connected:
            return
        was_connected = self.metrics.connects > 0
        await handle.connect()
        self.metrics.connects += 1
        if was_connected:
            self.metrics.reconnects += 1
            self._emit("mcp.server.reconnected", {"server_id": handle.config.server_id})
        else:
            self._emit(
                "mcp.server.connected",
                {
                    "server_id": handle.config.server_id,
                    "protocol_version": handle.protocol_version,
                    "server_info": handle.server_info,
                    "config_version": handle.config.config_version,
                },
            )

    # ------------------------------------------------------------------- call

    async def call_tool(
        self,
        server_id: str,
        remote_name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        """Invoke one remote tool and return the raw ``CallToolResult``."""
        config = self.config(server_id)
        handle = self.handle(server_id)
        await self._ensure_session(handle)
        deadline = timeout_seconds or config.timeout_seconds
        session = handle.session_or_raise()
        self.metrics.call_requests += 1
        try:
            return await asyncio.wait_for(
                session.call_tool(
                    remote_name,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=deadline * _CALL_DEADLINE_RATIO),
                ),
                timeout=deadline,
            )
        except asyncio.TimeoutError as exc:
            self.metrics.call_failures += 1
            handle.mark_broken(f"{type(exc).__name__}: {exc}")
            raise McpDisconnected(f"MCP tool {server_id}/{remote_name} timed out") from exc
        except McpError as exc:
            self.metrics.call_failures += 1
            if isinstance(exc, McpDisconnected):
                handle.mark_broken(str(exc))
                raise
            # The SDK reports its own read timeout as a generic McpError, so
            # "no answer arrived" has to be recovered from the message rather
            # than assumed to be a protocol error.
            raise self._fail_call(handle, server_id, remote_name, exc) from exc
        except Exception as exc:  # noqa: BLE001 - SDK error hierarchy is broad
            self.metrics.call_failures += 1
            raise self._fail_call(handle, server_id, remote_name, exc) from exc

    def _fail_call(
        self, handle: _SessionHandle, server_id: str, remote_name: str, exc: Exception,
    ) -> McpError:
        error = self._classify_call_error(server_id, remote_name, exc)
        if isinstance(error, McpDisconnected):
            handle.mark_broken(str(error))
        return error

    @staticmethod
    def _classify_call_error(server_id: str, remote_name: str, exc: Exception) -> McpError:
        """Separate "no answer" from "answered badly".

        The distinction matters: a timeout or a dropped connection after a
        remote write means the effect may have landed, while a protocol error
        means the server answered and the answer was unusable.
        """
        message = f"{type(exc).__name__}: {exc}"
        if _is_timeout_error(message):
            return McpDisconnected(f"MCP tool {server_id}/{remote_name} timed out: {message}")
        if _is_disconnect_error(message):
            return McpDisconnected(
                f"MCP tool {server_id}/{remote_name} lost its connection: {message}"
            )
        return McpProtocolError(f"MCP tool {server_id}/{remote_name} failed: {message}")


def _describe_exception(exc: BaseException) -> str:
    """Flatten anyio/task-group wrappers down to the first real cause.

    The SDK runs transports inside task groups, so a failed spawn surfaces as
    ``ExceptionGroup: unhandled errors in a TaskGroup``. That tells an operator
    nothing; the innermost cause is what identifies the problem. Transport
    errors are unhelpful in a second way - httpx ``ConnectError`` often carries
    an empty message and keeps the actual reason (DNS failure, refused socket)
    on its ``__cause__`` - so the chain is walked until something says
    something, and ``repr`` is the last resort rather than a bare class name.
    """
    current: BaseException = exc
    for _ in range(8):
        nested = getattr(current, "exceptions", None)
        if not isinstance(current, BaseExceptionGroup) or not nested:
            break
        current = nested[0]
    probe: BaseException = current
    for _ in range(8):
        if str(probe).strip():
            return f"{type(probe).__name__}: {probe}"
        next_cause = probe.__cause__ or probe.__context__
        if next_cause is None:
            break
        probe = next_cause
    return f"{type(current).__name__}: {current!r}"


def _is_timeout_error(message: str) -> bool:
    lowered = message.lower()
    return "timeout" in lowered or "timed out" in lowered


def _is_disconnect_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in ("closedresourceerror", "brokenresource", "endofstream", "connection", "closed")
    )


def load_manager_from_env(*, metrics: McpMetrics | None = None) -> McpClientManager:
    """Build a manager from ``BETTER_AGENT_MCP_SERVERS``.

    A broken entry is reported and dropped, so a broken optional integration
    cannot stop the application from starting and cannot disable the servers
    that are configured correctly. The rejections are kept on the manager so
    the acceptance report and ``/health``-style surfaces can show them.
    """
    raw = os.getenv(MCP_SERVERS_ENV)
    configs, rejections = parse_server_configs_lenient(raw)
    for rejection in rejections:
        logger.error(
            "MCP server entry %s (%s) rejected: %s",
            rejection.index, rejection.server_id or "no server_id", rejection.reason,
        )
    manager = McpClientManager(configs, metrics=metrics)
    manager.config_rejections = tuple(rejections)
    return manager
