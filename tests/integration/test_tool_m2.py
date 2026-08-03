from __future__ import annotations

import asyncio
from uuid import UUID

from agent_core.adapters.determinism import RandomIdFactory
from agent_core.adapters.persistence.queue import PostgresRunQueue
from agent_core.bootstrap import build
from agent_core.domain.agents import AgentSpec
from agent_core.domain.messages import TextPart, ToolCallItem
from agent_core.domain.policies import (
    IdempotencyClass,
    RiskLevel,
    SideEffectClass,
    TrustLevel,
)
from agent_core.domain.runs import RunCheckpoint, RunStatus, Step
from agent_core.domain.tools import ToolExecutionContext, ToolResult, ToolSpec
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.executor import ToolPipeline
from agent_core.tools.registry import StaticToolRegistry
from tests.integration.m2_support import PRINCIPAL, database_settings


class CountingTool:
    def __init__(self) -> None:
        self.executions = 0
        self.spec = ToolSpec(
            name="demo.count",
            version="1.0.0",
            description="Count executions.",
            input_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            output_schema=None,
            side_effect=SideEffectClass.NONE,
            risk=RiskLevel.LOW,
            idempotency=IdempotencyClass.READ_ONLY,
            timeout_seconds=5,
            maximum_output_bytes=4096,
            allow_parallel=False,
            output_trust=TrustLevel.INTERNAL_TOOL,
        )

    async def execute(
        self, arguments: dict[str, object], context: ToolExecutionContext
    ) -> ToolResult:
        del arguments, context
        self.executions += 1
        await asyncio.sleep(0)
        return ToolResult(ok=True, content=[TextPart(text="once")])


async def test_concurrent_duplicate_tool_calls_execute_once_and_resume_is_idempotent() -> None:
    tool = CountingTool()
    registry = StaticToolRegistry()
    registry.register(tool)
    async with build(settings=database_settings(), storage="postgres") as composition:
        run_id = await composition.runs.submit("call the counting tool")
        async with composition.uow_factory() as uow:
            assert isinstance(uow.queue, PostgresRunQueue)
            claimed = await uow.queue.claim("tool-worker", [0])
        assert claimed is not None
        pipeline = ToolPipeline(
            registry,
            composition.uow_factory,
            composition.clock,
            RandomIdFactory(),
        )
        call = ToolCallItem(
            call_id="same-call",
            item_index=0,
            name=tool.spec.name,
            arguments={"value": 1},
            raw_arguments='{"value":1}',
        )
        checkpoint = RunCheckpoint(
            run_id=run_id,
            version=0,
            status=RunStatus.RUNNING,
            created_at=composition.clock.now(),
        )
        step = Step(run_id=run_id, step_number=1, started_at=composition.clock.now())
        token = RunCancellationToken(composition.clock, None)
        configured_agent = await _agent_for(composition.uow_factory, claimed.run.agent_id)
        configured_agent = configured_agent.model_copy(
            update={"enabled_tools": [tool.spec.name]}, deep=True
        )

        async def invoke() -> object:
            return await pipeline.dispatch(
                run=claimed.run,
                checkpoint=checkpoint,
                tool_calls=[call],
                principal=PRINCIPAL,
                step=step,
                agent=configured_agent,
                token=token,
                lease=claimed.lease,
            )

        first, second = await asyncio.gather(invoke(), invoke())
        assert first == second
        restarted = ToolPipeline(
            registry,
            composition.uow_factory,
            composition.clock,
            RandomIdFactory(),
        )
        third = await restarted.dispatch(
            run=claimed.run,
            checkpoint=checkpoint,
            tool_calls=[call],
            principal=PRINCIPAL,
            step=step,
            agent=configured_agent,
            token=token,
            lease=claimed.lease,
        )
        assert third == first
        assert tool.executions == 1
        assert pipeline._key_locks == {}
        assert restarted._key_locks == {}
        events = await composition.runs.events(run_id)
        assert [event.event_type for event in events].count("tool.call.proposed") == 1


async def _agent_for(factory: UnitOfWorkFactory, agent_id: UUID) -> AgentSpec:
    async with factory() as uow:
        return await uow.agents.latest_version(agent_id)
