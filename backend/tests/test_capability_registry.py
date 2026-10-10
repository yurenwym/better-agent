from app.capability_registry import CapabilityRegistry
from app.tool_contracts import ToolSpec, ToolResult, ToolRisk
import pytest


def test_catalog_preserves_order_and_rejects_duplicate_definitions():
    registry = CapabilityRegistry()
    for name in ("second", "first"):
        registry.register(ToolSpec(name, name, {"type": "object"}, ToolRisk.READ,
                                   lambda _: ToolResult(True, "ok")))
    assert [item["function"]["name"] for item in registry.describe()] == ["second", "first"]
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(registry.spec("first"))
    registry.unregister("second")
    assert registry.find("second") is None
    assert tuple(spec.name for spec in registry.specs()) == ("first",)


def test_contract_and_catalog_have_no_infrastructure_imports():
    import ast
    import inspect
    from app import capability_registry, tool_contracts
    for module in (capability_registry, tool_contracts):
        imports = [node.module for node in ast.walk(ast.parse(inspect.getsource(module)))
                   if isinstance(node, ast.ImportFrom)]
        assert not {"db", "domain", "tools", "tool_executor", "model_gateway"}.intersection(imports)
