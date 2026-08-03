from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import agent, principal


def test_registry_resolves_and_filters_the_pinned_advertisement() -> None:
    registry = StaticToolRegistry()
    tool = CalculatorTool()
    registry.register(tool)
    assert registry.get("math.calculate").spec == tool.spec
    specs = registry.specs_for_session(agent(), principal(), object(), object())
    assert [spec.name for spec in specs] == ["math.calculate"]
