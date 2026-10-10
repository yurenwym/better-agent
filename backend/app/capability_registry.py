"""Ordered capability catalog. Registration never performs execution or I/O."""
from typing import Any
from .tool_contracts import ToolSpec, ToolRejected


class CapabilityRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def unregister(self, name: str) -> None:
        """Remove a definition when refreshing its source's catalog."""
        self._tools.pop(name, None)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.model_schema if spec.model_schema is not None else spec.schema,
                },
            }
            for spec in self._tools.values()
        ]

    def spec(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolRejected("unknown tool")
        return spec

    def find(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)
