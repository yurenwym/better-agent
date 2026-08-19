from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


class RouteProtocolError(ValueError):
    """The model did not produce the bounded conversation response protocol."""


@dataclass(frozen=True)
class RouteDecision:
    policy: str
    content_shape: str = ""
    reason_code: str = ""


class ControlHeadDecoder:
    """Decode one bounded JSON control line without exposing it as Markdown."""

    _policies = {"answer", "propose_execution", "clarify"}

    def __init__(self, max_header_bytes: int = 1024) -> None:
        self.max_header_bytes = max_header_bytes
        self._buffer = ""
        self.header: RouteDecision | None = None

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        if self.header is not None:
            return chunk
        self._buffer += chunk
        if len(self._buffer.encode("utf-8")) > self.max_header_bytes:
            raise RouteProtocolError("conversation control header is too long")
        newline = self._buffer.find("\n")
        if newline < 0:
            return ""
        line = self._buffer[:newline].rstrip("\r")
        remainder = self._buffer[newline + 1 :]
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RouteProtocolError("conversation control header is invalid") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise RouteProtocolError("conversation control header version is invalid")
        if set(payload) - {"v", "policy", "content_shape", "reason_code"}:
            raise RouteProtocolError("conversation control header has unknown fields")
        policy = payload.get("policy")
        if policy not in self._policies:
            raise RouteProtocolError("conversation policy is invalid")
        content_shape = payload.get("content_shape", "")
        reason_code = payload.get("reason_code", "")
        if not isinstance(content_shape, str) or not isinstance(reason_code, str):
            raise RouteProtocolError("conversation control header fields are invalid")
        self.header = RouteDecision(policy, content_shape, reason_code)
        self._buffer = ""
        return remainder

    def finish(self) -> RouteDecision:
        if self.header is None:
            raise RouteProtocolError("conversation control header is missing")
        return self.header


class RouteAndRespondModel(Protocol):
    async def route_and_respond(
        self,
        *,
        content: str,
        history: list[dict[str, str]],
        skill_names: list[str],
        on_text_delta,
        on_text_reset,
        cancel_event,
    ) -> Any: ...
