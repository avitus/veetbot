from agent_core.tools.calculator import CalculatorTool
from tests.contract.support import tool_context


async def test_tool_returns_one_of_the_two_declared_result_shapes() -> None:
    tool = CalculatorTool()
    success = await tool.execute({"expression": "17 * 23"}, tool_context())
    failure = await tool.execute({"expression": "1 / 0"}, tool_context())
    assert success.ok and success.failure is None
    assert not failure.ok and failure.failure is not None
