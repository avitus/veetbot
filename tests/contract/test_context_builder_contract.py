from agent_core.context.builder import MinimalContextBuilder
from agent_core.domain.messages import TextPart, UserMessage
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW, agent, memory_stack, principal, run


async def test_context_builder_is_deterministic_and_places_change_in_region_b() -> None:
    clock, _sessions, _runs, _events = await memory_stack()
    registry = StaticToolRegistry()
    registry.register(CalculatorTool())
    builder = MinimalContextBuilder(registry, clock)
    checkpoint = RunCheckpoint(
        run_id=run().id,
        version=1,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text="first")])],
        created_at=NOW,
    )
    first = await builder.build(run(), checkpoint, agent(), principal())
    checkpoint.conversation.append(UserMessage(content=[TextPart(text="second")]))
    second = await builder.build(run(), checkpoint, agent(), principal())
    assert first.metadata["prefix_sha256"] == second.metadata["prefix_sha256"]
    assert first.metadata["body_sha256"] != second.metadata["body_sha256"]
