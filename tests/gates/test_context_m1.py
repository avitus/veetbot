"""Milestone 1 minimal-context hard gate."""

from __future__ import annotations

import asyncio

from hypothesis import given
from hypothesis import strategies as st

from agent_core.adapters.determinism import FixedClock
from agent_core.context.builder import MinimalContextBuilder
from agent_core.domain.messages import ModelRequest, SystemMessage, TextPart, UserMessage
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW, agent, principal, run


@given(prompt=st.text(min_size=1, max_size=100))
def test_determinism(prompt: str) -> None:
    clock = FixedClock(NOW)
    registry = StaticToolRegistry()
    registry.register(CalculatorTool())
    registry.register(CurrentTimeTool(clock))
    builder = MinimalContextBuilder(registry, clock)
    active_run = run(status=RunStatus.RUNNING)
    checkpoint = RunCheckpoint(
        run_id=active_run.id,
        version=0,
        status=RunStatus.RUNNING,
        conversation=[UserMessage(content=[TextPart(text=prompt)])],
        created_at=NOW,
    )

    async def build_twice() -> tuple[ModelRequest, ModelRequest]:
        first = await builder.build(active_run, checkpoint, agent(), principal())
        second = await builder.build(active_run, checkpoint, agent(), principal())
        return first, second

    first, second = asyncio.run(build_twice())
    assert first.model_dump_json() == second.model_dump_json()
    prefix_items = first.conversation[:3]
    assert all(isinstance(item, SystemMessage) for item in prefix_items)
    assert [item.trust.value for item in prefix_items if isinstance(item, SystemMessage)] == [
        "platform",
        "trusted_configuration",
        "trusted_configuration",
    ]
    runtime_items = [
        item
        for item in first.conversation
        if item.kind == "user"
        and item.content
        and getattr(item.content[0], "text", "").startswith("Runtime metadata")
    ]
    assert len(runtime_items) == 1
    assert runtime_items[0].trust.value == "platform"
