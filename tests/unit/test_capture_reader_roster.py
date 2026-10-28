"""ADR-0261: the capture reader is pinned as an unindexed deferral and costs no prompt."""

from __future__ import annotations

import copy
from typing import Any

from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.planner import EventContextPlanner
from agent_core.context.rendering import build_prefix
from agent_core.domain.agents import DEFERRED_TOOLS_METADATA_KEY, AgentSpec
from agent_core.domain.context import ContextPlan
from agent_core.tools.artifact_read_capture import ArtifactReadCaptureTool
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.demo_external_write import DemoExternalWriteTool
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.tool_call import ToolCallTool
from tests.contract.support import agent, principal, session
from tests.unit.test_history_cache_window import CONFIG, _claude, _factory

READER = "artifact.read_capture"
BASE = ["math.calculate", "system.current_time", "demo.external_write"]


def _agent(enabled: list[str], deferred: list[str]) -> AgentSpec:
    return agent().model_copy(
        update={
            "enabled_tools": enabled,
            "metadata": {DEFERRED_TOOLS_METADATA_KEY: deferred} if deferred else {},
        }
    )


async def _plan(spec: AgentSpec, config: dict[str, Any] = CONFIG) -> ContextPlan:
    clock, factory = await _factory()
    registry = StaticToolRegistry()
    for tool in (
        CalculatorTool(),
        CurrentTimeTool(clock),
        DemoExternalWriteTool(),
        ToolCallTool(),
        ArtifactReadCaptureTool(factory, object(), clock),  # type: ignore[arg-type]
    ):
        registry.register(tool)
    owner = principal().model_copy(update={"scopes": {"demo.write"}})
    planner = EventContextPlanner(
        factory,
        registry,
        ConservativeTokenEstimator(),
        clock,
        owner,
        config,
        policy_version="contract-policy@1",
    )
    return await planner.plan(session(), spec, owner, _claude())


def _rendered(spec: AgentSpec, plan: ContextPlan, *, without_reader: bool = False) -> list[Any]:
    deferred = [
        tool for tool in plan.deferred_tool_specs if not (without_reader and tool.name == READER)
    ]
    return build_prefix(
        spec,
        plan.tool_specs,
        plan.skill_catalog,
        plan.memory_snapshot,
        persona=plan.persona_text,
        deferred_tools=deferred,
    )


async def test_the_reader_is_pinned_without_a_definition_or_an_index_entry() -> None:
    with_reader = _agent([*BASE, "tool.call", READER], ["demo.external_write", READER])
    without = _agent([*BASE, "tool.call"], ["demo.external_write"])
    plan = await _plan(with_reader)
    baseline = await _plan(without)

    assert READER in plan.deferred_tool_names
    assert READER not in plan.tool_names
    assert plan.tool_names == baseline.tool_names
    assert plan.skipped_tool_names == baseline.skipped_tool_names == ()
    assert [name for name in plan.deferred_tool_names if name != READER] == list(
        baseline.deferred_tool_names
    )
    # Zero prompt bytes: the rendered prefix is the baseline's, item for item.
    rendered = _rendered(with_reader, plan)
    assert rendered == _rendered(without, baseline)
    assert rendered == _rendered(with_reader, plan, without_reader=True)
    assert READER not in str([item.model_dump(mode="json") for item in rendered])
    assert plan.prefix_tokens == baseline.prefix_tokens


async def test_the_reader_takes_no_share_of_the_index_caps() -> None:
    config = copy.deepcopy(CONFIG)
    config["classes"]["deferred_tool_index"]["max_items"] = 1
    plan = await _plan(
        _agent([*BASE, "tool.call", READER], ["demo.external_write", READER]), config
    )
    assert set(plan.deferred_tool_names) == {"demo.external_write", READER}
    assert plan.skipped_tool_names == ()


async def test_an_enabled_reader_is_never_a_definition() -> None:
    plan = await _plan(_agent([*BASE, "tool.call", READER], []))
    assert READER not in plan.tool_names
    assert READER in plan.deferred_tool_names
    assert "tool.call" in plan.tool_names


async def test_without_tool_call_the_reader_is_skipped() -> None:
    plan = await _plan(_agent([*BASE, READER], [READER]))
    assert READER not in plan.tool_names
    assert plan.deferred_tool_names == ()
    assert plan.skipped_tool_names == (READER,)
