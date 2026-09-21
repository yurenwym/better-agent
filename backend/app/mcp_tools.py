"""MCP tool definitions mapped onto the Harness Tool Registry.

Responsibilities kept here, and nowhere else:

* turn a discovered remote tool definition into a :class:`~app.tools.ToolSpec`
  with a stable model-visible name, a local definition digest and the server
  identity that produced it;
* validate arguments against the tool's real JSON Schema dialect instead of the
  partial checks the native tools use;
* decide the risk of a remote tool from *local* policy, never from the server's
  own ``readOnlyHint``;
* map a ``CallToolResult`` onto the existing :class:`~app.tools.ToolResult`,
  including explicit capability failures for content the Harness cannot carry,
  and an artifact-backed size cap;
* build the approval binding for a remote write so the user's consent is bound
  to one server configuration version, one definition digest and one parameter
  set.

The executor, approval and reconciliation paths are the ones the native tools
already use; this module does not introduce a second execution ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .mcp_client import (
    CatalogSnapshot,
    McpClientManager,
    McpDisconnected,
    McpError,
    McpServerConfig,
    RemoteTool,
)
from .tools import (
    ToolExecutionContext,
    ToolReconciliationRequired,
    ToolRegistry,
    ToolRejected,
    ToolResult,
    ToolRisk,
    ToolSpec,
)

logger = logging.getLogger(__name__)

MCP_SOURCE = "mcp"

# OpenAI-compatible function names are limited to this alphabet and length.
MODEL_TOOL_NAME_MAX = 64
MCP_NAME_PREFIX = "mcp__"

_TEXT_BLOCK = "text"

_UNSUPPORTED_CONTENT_MESSAGE = (
    "该工具返回了当前版本无法承载的内容块（{types}）。"
    "结果没有被静默丢弃，但也不能当作成功结果使用。"
)

# Protocol result types this phase deliberately does not implement. Answering
# them would mean inventing a user decision.
_UNSUPPORTED_PROTOCOL_RESULTS = {"input_required", "elicitation", "task"}


class McpToolRejected(ToolRejected):
    """The tool definition or its arguments cannot be used by the Harness."""


def _slug(value: str) -> str:
    out: list[str] = []
    for char in value:
        if char.isascii() and (char.isalnum() or char in "-_"):
            out.append(char)
        else:
            out.append("_")
    return "".join(out) or "_"


def model_tool_name(server_id: str, remote_name: str) -> str:
    """Deterministic, provider-safe model-visible name.

    The mapping is a pure function of the remote identity, so two processes
    agree on it without sharing state and a name never drifts between the turn
    that proposed a call and the turn that resumes it.
    """
    identity = f"{server_id}\x00{remote_name}"
    base = f"{MCP_NAME_PREFIX}{_slug(server_id)}__{_slug(remote_name)}"
    if len(base) <= MODEL_TOOL_NAME_MAX:
        return base
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    keep = MODEL_TOOL_NAME_MAX - len(digest) - 1
    return f"{base[:keep]}_{digest}"


def _schema_dialect(schema: dict[str, Any]) -> type:
    from jsonschema.validators import validator_for

    try:
        return validator_for(schema)
    except Exception as exc:  # noqa: BLE001 - an unusable $schema is a rejection
        raise McpToolRejected(f"unsupported JSON Schema dialect: {exc}") from exc


def _check_schema(schema: dict[str, Any]) -> None:
    """Reject definitions the Harness cannot enforce.

    ``check_schema`` validates the schema itself. Reference resolution happens
    at call time against an empty registry, so an unresolvable ``$ref`` fails
    the call instead of silently passing: the validator never fetches an
    uncontrolled URL. ``$defs``/``$ref`` semantics are preserved rather than
    stripped, because deleting a constraint is worse than refusing a tool.
    """
    from jsonschema.exceptions import SchemaError

    if not isinstance(schema, dict):
        raise McpToolRejected("tool input schema must be an object")
    if schema.get("type") not in (None, "object"):
        raise McpToolRejected(
            "model providers require an object-rooted tool schema; "
            f"got type={schema.get('type')!r}"
        )
    validator = _schema_dialect(schema)
    try:
        validator.check_schema(schema)
    except SchemaError as exc:
        raise McpToolRejected(f"tool input schema is not valid: {str(exc).splitlines()[0]}") from exc


def validate_arguments(schema: dict[str, Any], params: dict[str, Any]) -> None:
    """Validate arguments with the tool's own dialect, without network access."""
    from jsonschema.exceptions import ValidationError

    validator = _schema_dialect(schema)(schema)
    try:
        validator.validate(params)
    except ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        raise McpToolRejected(f"{location}: {exc.message}") from exc
    except Exception as exc:  # noqa: BLE001 - unresolved $ref, bad dialect, ...
        raise McpToolRejected(f"tool arguments could not be validated: {exc}") from exc


def _unsupported_result_type(meta: dict[str, Any]) -> str | None:
    """Detect protocol features this phase deliberately does not implement."""
    result_type = meta.get("resultType")
    if isinstance(result_type, str) and result_type != "complete":
        return result_type
    if meta.get("inputResponses") is not None or meta.get("requestState") is not None:
        return "input_required"
    return None


def _public_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Keep protocol metadata small and free of anything secret-looking."""
    public: dict[str, Any] = {}
    for key, value in meta.items():
        if key in {"inputResponses", "requestState", "authorization", "Authorization"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            public[key] = value
    return public


@dataclass(frozen=True)
class McpCallMapping:
    """The outcome of mapping one remote result, before it becomes a ToolResult."""

    ok: bool
    summary: str
    data: dict[str, Any]
    error: str | None
    meta: dict[str, Any]


def map_call_result(result: Any, *, server_id: str, remote_name: str) -> McpCallMapping:
    """Map a ``CallToolResult`` onto the Harness result vocabulary."""
    meta = dict(getattr(result, "meta", None) or {})
    base_meta = {"source": MCP_SOURCE, "server_id": server_id, "remote_name": remote_name}
    unsupported_protocol = _unsupported_result_type(meta)
    if unsupported_protocol is not None:
        return McpCallMapping(
            ok=False,
            summary="远端返回了当前版本未启用的协议续接请求，未自动应答。",
            data={"result_type": unsupported_protocol, "raw_meta": _public_meta(meta)},
            error="MCP_INPUT_REQUIRED_UNSUPPORTED",
            meta={**base_meta, "result_type": unsupported_protocol},
        )

    texts: list[str] = []
    unsupported_types: list[str] = []
    for block in getattr(result, "content", None) or ():
        block_type = getattr(block, "type", None)
        if block_type == _TEXT_BLOCK:
            texts.append(str(getattr(block, "text", "") or ""))
        elif block_type:
            unsupported_types.append(str(block_type))
    structured = getattr(result, "structuredContent", None)
    is_error = bool(getattr(result, "isError", False))
    text = "\n".join(part for part in texts if part)

    data: dict[str, Any] = {}
    if text:
        data["text"] = text
    if structured is not None:
        data["structured"] = structured

    if unsupported_types:
        unique = sorted(set(unsupported_types))
        return McpCallMapping(
            ok=False,
            summary=_UNSUPPORTED_CONTENT_MESSAGE.format(types=", ".join(unique)),
            data={**data, "unsupported_content_types": unique},
            error="MCP_UNSUPPORTED_CONTENT",
            meta={**base_meta, "unsupported_content_types": unique},
        )
    if is_error:
        return McpCallMapping(
            ok=False,
            summary=(text[:400] or "远端工具报告了错误。"),
            data=data,
            error="MCP_TOOL_ERROR",
            meta=base_meta,
        )
    if not data:
        return McpCallMapping(
            ok=True, summary="远端工具没有返回内容。", data={}, error=None, meta=base_meta,
        )
    return McpCallMapping(
        ok=True,
        summary=text[:400] if text else "远端工具返回了结构化结果。",
        data=data,
        error=None,
        meta=base_meta,
    )


class McpToolAdapter:
    """Registers one server's catalogue into the shared registry and calls it."""

    def __init__(
        self,
        manager: McpClientManager,
        config: McpServerConfig,
        registry: ToolRegistry,
        *,
        artifact_root: Path | None = None,
    ) -> None:
        self.manager = manager
        self.config = config
        self.registry = registry
        self.artifact_root = self._resolve_artifact_root(artifact_root)
        self._registered: dict[str, RemoteTool] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._auth_scope = "public"
        self._lock = asyncio.Lock()
        self.rejected_tools: dict[str, str] = {}
        # Countable evidence for the acceptance report.
        self.registrations = 0
        self.refreshes = 0
        self.definition_changes = 0

    def _resolve_artifact_root(self, artifact_root: Path | None) -> Path:
        """Artifacts must live inside the registry workspace.

        ``ToolResult.artifact_ref`` is a workspace-relative path everywhere
        else in the Harness, and the workspace is what bounds what a later read
        may touch. A root outside it would produce a reference that no other
        part of the system can resolve safely, so it is rejected up front.
        """
        root = Path(artifact_root).resolve() if artifact_root is not None else (
            self.registry.workspace / "mcp-results"
        )
        try:
            root.relative_to(self.registry.workspace)
        except ValueError as exc:
            raise ValueError(
                f"MCP artifact root {root} must stay inside the tool workspace "
                f"{self.registry.workspace}"
            ) from exc
        return root

    @property
    def server_id(self) -> str:
        return self.config.server_id

    @property
    def auth_scope(self) -> str:
        return self._auth_scope

    def model_names(self) -> tuple[str, ...]:
        return tuple(self._registered)

    def remote_name_for(self, model_name: str) -> str | None:
        tool = self._registered.get(model_name)
        return tool.name if tool is not None else None

    def digest_for(self, model_name: str) -> str | None:
        tool = self._registered.get(model_name)
        return tool.definition_digest if tool is not None else None

    # ----------------------------------------------------------- registration

    def _select(self, tools: tuple[RemoteTool, ...]) -> tuple[list[RemoteTool], dict[str, str]]:
        """Apply the local enable-list and risk policy to a discovered catalogue."""
        enabled = self.config.enabled_tools
        selected: list[RemoteTool] = []
        rejected: dict[str, str] = {}
        for tool in tools:
            if enabled is not None and tool.name not in enabled:
                rejected[tool.name] = "not in the local enabled_tools whitelist"
                continue
            if tool.task_support == "required":
                rejected[tool.name] = "requires protocol Tasks, which this phase does not enable"
                continue
            if self.config.risk_for(tool.name) == ToolRisk.WRITE.value and not self.config.allow_write_tools:
                rejected[tool.name] = "resolves to WRITE but this server is not trusted for side effects"
                continue
            selected.append(tool)
        return selected, rejected

    def build_specs(
        self, snapshot: CatalogSnapshot, *, taken: set[str] | None = None,
    ) -> tuple[list[ToolSpec], dict[str, str]]:
        """Build the specs for one catalogue snapshot. Pure; registers nothing."""
        selected, rejected = self._select(snapshot.tools())
        used = set(taken or ())
        specs: list[ToolSpec] = []
        for tool in selected:
            name = model_tool_name(self.server_id, tool.name)
            if name in used:
                rejected[tool.name] = f"model-visible name {name} is already in use"
                continue
            try:
                _check_schema(tool.input_schema)
            except McpToolRejected as exc:
                rejected[tool.name] = str(exc)
                continue
            used.add(name)
            specs.append(self._spec_for(name, tool))
        return specs, rejected

    def _spec_for(self, model_name: str, tool: RemoteTool) -> ToolSpec:
        risk = ToolRisk(self.config.risk_for(tool.name))
        return ToolSpec(
            name=model_name,
            description=self._description(tool),
            schema=tool.input_schema,
            risk=risk,
            handler=self._sync_unavailable,
            context_handler=self._make_handler(model_name, tool),
            validator=self._make_validator(model_name),
            timeout_seconds=self.config.timeout_seconds,
            recover_result=self._make_recover(model_name, tool) if tool.name in self.config.receipt_tools else None,
            source=MCP_SOURCE,
            server_id=self.server_id,
            remote_name=tool.name,
            definition_digest=tool.definition_digest,
        )

    def _description(self, tool: RemoteTool) -> str:
        head = tool.description.strip() or f"MCP 工具 {tool.name}（来自 {self.server_id}）。"
        return (
            f"{head}\n\n[来源：MCP 服务 {self.server_id}；远端工具名 {tool.name}；"
            f"定义摘要 {tool.definition_digest[:12]}]"
        )

    def _sync_unavailable(self, params: dict[str, Any]) -> ToolResult:
        return ToolResult(
            False,
            "MCP 工具只能通过异步执行路径调用。",
            error="MCP_ASYNC_ONLY",
            meta={"source": MCP_SOURCE, "server_id": self.server_id},
        )

    async def sync(self, auth_scope: str, *, force: bool = False) -> dict[str, Any]:
        """Refresh the catalogue and replace this server's registered tools.

        The replacement is atomic: the whole new catalogue is validated first
        and only then swapped in, so a half-updated registry is never visible
        to a model call.
        """
        async with self._lock:
            self._auth_scope = auth_scope
            snapshot = await self.manager.ensure_catalog(self.server_id, auth_scope, force=force)
            self.refreshes += 1
            for name in self._registered:
                self.registry.unregister(name)
            self._registered = {}
            self._schemas = {}
            specs, rejected = self.build_specs(snapshot, taken=self._taken_excluding_self())
            for spec in specs:
                self.registry.register(spec)
                remote = snapshot.tool(spec.remote_name or "")
                if remote is None:  # pragma: no cover - snapshot is immutable here
                    continue
                self._registered[spec.name] = remote
                self._schemas[spec.name] = spec.schema
            self.registrations += 1
            self.rejected_tools = rejected
            if rejected:
                logger.info(
                    "MCP server %s rejected %d tool(s): %s",
                    self.server_id, len(rejected), sorted(rejected),
                )
            if self.config.enabled_tools is None and len(specs) > 10:
                # The whole catalogue is cached either way, but everything
                # registered here is also offered to the model on every turn.
                logger.warning(
                    "MCP server %s registers %d tools with no enabled_tools whitelist; "
                    "every one of them is injected into the model context",
                    self.server_id, len(specs),
                )
            return {
                "server_id": self.server_id,
                "registered": len(specs),
                "whitelisted": self.config.enabled_tools is not None,
                "rejected": dict(rejected),
                "cache_scope": snapshot.cache_scope,
                "pages": len(snapshot.pages),
                "protocol_version": snapshot.protocol_version,
            }

    def _taken_excluding_self(self) -> set[str]:
        return {spec.name for spec in self.registry.specs() if spec.name not in self._registered}

    async def ensure_current(self, model_name: str) -> RemoteTool:
        """Re-verify one tool's definition before the Harness acts on it.

        A definition that changed since the model saw it must not run under an
        approval the user gave for the old one.
        """
        previous = self._registered.get(model_name)
        if previous is None:
            raise McpToolRejected("unknown MCP tool")
        snapshot = await self.manager.ensure_catalog(self.server_id, self._auth_scope)
        current = snapshot.tool(previous.name)
        if current is None:
            self.manager.invalidate(self.server_id, self._auth_scope)
            raise McpToolRejected(f"远端工具 {previous.name} 已不存在；已刷新目录，请重新发起调用。")
        if current.definition_digest != previous.definition_digest:
            self.definition_changes += 1
            self.manager.invalidate(self.server_id, self._auth_scope)
            await self.sync(self._auth_scope, force=True)
            raise McpToolRejected(
                f"远端工具 {previous.name} 的定义已变化；旧的定义摘要不再有效，请重新发起调用。"
            )
        return current

    def validate(self, model_name: str, params: dict[str, Any]) -> None:
        """Call-time argument validation, independent of the native checks."""
        schema = self._schemas.get(model_name)
        if schema is None:
            raise McpToolRejected("unknown MCP tool")
        validate_arguments(schema, params)
        violation = self._resource_violation(model_name, params)
        if violation is not None:
            raise McpToolRejected(violation)

    def _resource_violation(self, model_name: str, params: dict[str, Any]) -> str | None:
        """Resolve the resource scope from configuration, not from the model.

        A parameter the model chose - a repository name, an account, a project -
        is only reachable when the local configuration already authorised that
        value. The model cannot widen its own scope by asking nicely.
        """
        remote_name = self.remote_name_for(model_name)
        if remote_name is None:
            return "unknown MCP tool"
        constraints = self.config.resource_allow.get(remote_name)
        if not constraints:
            return None
        for parameter, allowed in constraints.items():
            if parameter not in params:
                return f"参数 {parameter} 缺失，无法确认资源范围"
            if str(params[parameter]) not in allowed:
                return f"参数 {parameter} 的值不在本地授权的资源范围内"
        return None

    def _make_validator(self, model_name: str):
        """Bound to the tool identity, so the registry calls the right schema."""
        def validator(params: dict[str, Any]) -> None:
            self.validate(model_name, params)

        return validator

    # -------------------------------------------------------------- execution

    def _make_handler(
        self, model_name: str, tool: RemoteTool,
    ) -> Callable[[dict[str, Any], ToolExecutionContext], Awaitable[ToolResult]]:
        async def handler(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
            try:
                current = await self.ensure_current(model_name)
            except McpToolRejected as exc:
                return ToolResult(
                    False, str(exc), error="MCP_DEFINITION_CHANGED",
                    meta={"source": MCP_SOURCE, "server_id": self.server_id, "remote_name": tool.name},
                )
            except McpError as exc:
                return self._failure(exc, model_name)
            stale = self._stale_approval(current, context.authorization)
            if stale is not None:
                return ToolResult(
                    False, stale, error="MCP_APPROVAL_STALE",
                    meta={
                        "source": MCP_SOURCE, "server_id": self.server_id,
                        "remote_name": current.name,
                        "definition_digest": current.definition_digest,
                    },
                )
            try:
                raw = await self.manager.call_tool(
                    self.server_id, current.name, params, timeout_seconds=self.config.timeout_seconds,
                )
            except McpDisconnected as exc:
                # A lost connection after a write may still have landed remotely.
                if ToolRisk(self.config.risk_for(tool.name)) == ToolRisk.WRITE:
                    raise ToolReconciliationRequired(
                        f"远端写入 {self.server_id}/{tool.name} 的连接中断，结果待确认。"
                    ) from exc
                return self._failure(exc, model_name)
            except McpError as exc:
                return self._failure(exc, model_name)
            return self._to_result(raw, current)

        return handler

    def _stale_approval(self, current: RemoteTool, authorization: dict[str, Any] | None) -> str | None:
        """Refuse a write whose consent no longer describes what would run.

        The approval row proves *a* user decision happened; it cannot prove the
        thing being executed is still the thing that was approved. A refreshed
        catalogue can silently replace the definition between the request and
        the resume, so the binding recorded at approval time is re-checked
        against the definition and configuration in force now.
        """
        binding = (authorization or {}).get("mcp")
        if not isinstance(binding, dict):
            return None
        if binding.get("server_id") != self.server_id:
            return "审批绑定的 MCP 服务与当前服务不一致，未执行。"
        if binding.get("config_version") != self.config.config_version:
            return "MCP 服务配置或凭据在审批后发生变化，原审批不再适用，未执行。"
        if binding.get("remote_name") != current.name:
            return "审批绑定的远端工具与当前工具不一致，未执行。"
        if binding.get("definition_digest") != current.definition_digest:
            return "远端工具定义在审批后发生变化，原审批不再适用，未执行。"
        return None

    def _failure(self, exc: McpError, model_name: str) -> ToolResult:
        if isinstance(exc, McpDisconnected):
            error = "MCP_TIMEOUT" if "timed out" in str(exc) else "MCP_DISCONNECTED"
        else:
            error = "MCP_PROTOCOL_ERROR"
        return ToolResult(
            False,
            f"MCP 调用失败：{exc}",
            error=error,
            meta={"source": MCP_SOURCE, "server_id": self.server_id, "model_tool_name": model_name},
        )

    def _to_result(self, raw: Any, tool: RemoteTool) -> ToolResult:
        mapping = map_call_result(raw, server_id=self.server_id, remote_name=tool.name)
        data, truncated, artifact_ref = self._cap_size(mapping.data, tool=tool)
        meta = {**mapping.meta, "definition_digest": tool.definition_digest}
        summary = mapping.summary
        if truncated is not None:
            meta.update(truncated)
            summary = (
                f"{summary}\n（结果 {truncated['original_bytes']} 字节超过上限 "
                f"{self.config.max_result_bytes} 字节，完整内容已保存为 artifact。）"
            )
        return ToolResult(
            mapping.ok, summary, data, artifact_ref=artifact_ref, error=mapping.error, meta=meta,
        )

    def _cap_size(
        self, data: dict[str, Any], *, tool: RemoteTool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        """Keep the model-visible payload bounded; keep the whole result on disk."""
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(encoded) <= self.config.max_result_bytes:
            return data, None, None
        digest = hashlib.sha256(encoded).hexdigest()[:16]
        directory = self.artifact_root / _slug(self.server_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_slug(tool.name)}-{digest}.json"
        if not path.exists():
            path.write_text(
                json.dumps(
                    {"server_id": self.server_id, "remote_name": tool.name,
                     "definition_digest": tool.definition_digest, "data": data},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        relative = str(path.relative_to(self.registry.workspace)).replace("\\", "/")
        truncated = {
            "truncated": True,
            "original_bytes": len(encoded),
            "max_result_bytes": self.config.max_result_bytes,
            "artifact_ref": relative,
            "excerpt": self._excerpt(data),
        }
        return {"truncated": True, "artifact_ref": relative, "excerpt": truncated["excerpt"]}, truncated, relative

    @staticmethod
    def _excerpt(data: dict[str, Any], limit: int = 2000) -> dict[str, Any]:
        excerpt: dict[str, Any] = {}
        text = data.get("text")
        if isinstance(text, str):
            excerpt["text"] = text[:limit]
        structured = data.get("structured")
        if structured is not None:
            encoded = json.dumps(structured, ensure_ascii=False)
            excerpt["structured_excerpt"] = encoded[:limit]
            excerpt["structured_truncated"] = len(encoded) > limit
        return excerpt

    # --------------------------------------------------------------- recovery

    def _make_recover(
        self, model_name: str, tool: RemoteTool,
    ) -> Callable[[dict[str, Any], ToolExecutionContext], Awaitable[ToolResult | None]]:
        receipt = self.config.receipt_tools.get(tool.name) or {}
        receipt_tool = receipt.get("tool")
        arguments = receipt.get("arguments") or {}

        async def recover(params: dict[str, Any], context: ToolExecutionContext) -> ToolResult | None:
            """Read-only reconciliation through a declared receipt tool.

            Recovery only happens when the server's configuration names a
            read-only receipt tool for the write. Without verifiable remote
            evidence the Harness must not assume the write did or did not land.
            """
            if not isinstance(receipt_tool, str) or not receipt_tool:
                return None
            try:
                raw = await self.manager.call_tool(
                    self.server_id, receipt_tool, dict(arguments),
                    timeout_seconds=self.config.timeout_seconds,
                )
            except McpError:
                return None
            mapping = map_call_result(raw, server_id=self.server_id, remote_name=receipt_tool)
            if not mapping.ok:
                return None
            return ToolResult(
                True,
                "远端写入状态已通过回执查询确认。",
                {"receipt": mapping.data, "original_params": params},
                meta={"source": MCP_SOURCE, "server_id": self.server_id, "reconciled_by": receipt_tool},
            )

        return recover

    # --------------------------------------------------------------- approval

    def approval_binding(self, model_name: str) -> dict[str, Any]:
        """Identity the user's consent is bound to for a remote write."""
        tool = self._registered.get(model_name)
        if tool is None:
            raise McpToolRejected("unknown MCP tool")
        return {
            "mcp": {
                "server_id": self.server_id,
                "config_version": self.config.config_version,
                "remote_name": tool.name,
                "definition_digest": tool.definition_digest,
                "protocol_version": self.manager.protocol_version(self.server_id),
            }
        }


class McpToolRegistrySync:
    """Keeps every configured server's tools registered in the shared registry."""

    def __init__(
        self,
        manager: McpClientManager,
        registry: ToolRegistry,
        configs: tuple[McpServerConfig, ...] | list[McpServerConfig],
        *,
        artifact_root: Path | None = None,
    ) -> None:
        self.manager = manager
        self.registry = registry
        self.adapters: dict[str, McpToolAdapter] = {
            config.server_id: McpToolAdapter(manager, config, registry, artifact_root=artifact_root)
            for config in configs
        }
        self.failures: dict[str, str] = {}

    def adapter_for_tool(self, model_name: str) -> McpToolAdapter | None:
        for adapter in self.adapters.values():
            if model_name in adapter.model_names():
                return adapter
        return None

    def enabled_model_names(self) -> set[str]:
        return {name for adapter in self.adapters.values() for name in adapter.model_names()}

    def approval_binding_for(self, model_name: str) -> dict[str, Any]:
        adapter = self.adapter_for_tool(model_name)
        if adapter is None:
            return {}
        return adapter.approval_binding(model_name)

    async def sync_all(self, auth_scope: str, *, force: bool = False) -> dict[str, Any]:
        """Register every reachable server. A failing server never blocks the rest."""
        report: dict[str, Any] = {}
        for server_id, adapter in self.adapters.items():
            if not self.manager.connected(server_id):
                failures = await self.manager.start([server_id])
                if server_id in failures:
                    self.failures[server_id] = failures[server_id]
                    report[server_id] = {"registered": 0, "unavailable": failures[server_id]}
                    continue
            try:
                report[server_id] = await adapter.sync(auth_scope, force=force)
                self.failures.pop(server_id, None)
            except McpError as exc:
                self.failures[server_id] = str(exc)
                report[server_id] = {"registered": 0, "error": str(exc)}
                logger.warning("MCP server %s discovery failed: %s", server_id, exc)
        return report

    async def refresh_connected(self, auth_scope: str, *, timeout: float = 3.0) -> dict[str, Any]:
        """Refresh catalogues for servers that are already connected.

        Used at the start of a turn so the model sees the current definitions.
        It never *connects*: a server that is down must not add its handshake
        timeout to every chat turn, and an unreachable optional server must not
        be able to break plain chat.
        """
        report: dict[str, Any] = {}
        for server_id, adapter in self.adapters.items():
            if not self.manager.connected(server_id):
                continue
            try:
                report[server_id] = await asyncio.wait_for(adapter.sync(auth_scope), timeout=timeout)
            except (McpError, asyncio.TimeoutError) as exc:
                # Keep the previously registered catalogue rather than failing
                # the turn; execution re-verifies the definition anyway.
                self.failures[server_id] = str(exc)
                report[server_id] = {"refreshed": False, "error": str(exc)}
                logger.info("MCP catalogue refresh skipped for %s: %s", server_id, exc)
        return report

    async def close(self) -> None:
        await self.manager.close()
