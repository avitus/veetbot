"""A derived snapshot cannot survive deletion of even an unrendered dependency."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, cast

import pytest
import yaml

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.planner import EventContextPlanner
from agent_core.domain.errors import RunCancelledError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import MemoryReviewOutcome, Sensitivity
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.runs import RunStatus
from agent_core.memory.formation import GovernedMemoryService
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.derived_memory_cases import service
from tests.contract.reconsolidation_summary_recall_cases import (
    summary_recall_inputs,
    summary_retriever,
)
from tests.contract.reconsolidation_surface_cases import owner
from tests.contract.support import NOW, agent, principal, run, session


async def summary_snapshot_erasure(
    factory: UnitOfWorkFactory,
    mode: Literal["source", "summary", "rejection", "rollback"] = "source",
) -> None:
    operation, originals = await summary_recall_inputs(factory, omit_last=True)
    clock = FixedClock(NOW)
    async with factory() as uow:
        await uow.runs.create(run(status=RunStatus.RUNNING))
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text()
    )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="test",
        memory_retriever=summary_retriever(factory),
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    frozen = await planner.plan(session(), agent(), principal(), model)
    citation = f"[m:{str(operation.id)[:8]}]"
    assert citation in frozen.memory_snapshot
    assert originals[-1].statement not in frozen.memory_snapshot
    if mode == "source":
        await GovernedMemoryService(factory, clock, RandomIdFactory(), principal()).delete(
            originals[-1].id
        )
    else:
        if mode == "rollback":

            @asynccontextmanager
            async def failing() -> AsyncIterator[RepositoryUnitOfWork]:
                async with factory() as uow:
                    yield uow

                    async def independent_input() -> None:
                        async with factory() as separate:
                            await separate.events.append(
                                NewEvent(
                                    session_id=session().id,
                                    run_id=None,
                                    event_type="user.message.created",
                                    actor_type="principal",
                                    actor_id=principal().principal_id,
                                    payload={"content": "independent input during rollback"},
                                )
                            )

                    await asyncio.wait_for(asyncio.create_task(independent_input()), timeout=5)
                    raise RuntimeError("abort generated erasure")

            with pytest.raises(RuntimeError, match="abort generated erasure"):
                await service(cast(UnitOfWorkFactory, failing)).delete(
                    owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="erase"
                )
            current = await planner.current(session().id)
            assert current is not None and citation in current.memory_snapshot
            async with factory() as uow:
                assert (await uow.runs.get(run().id, principal())).cancel_requested_at is None
                events = await uow.events.list_after(session().id, 0, principal())
                assert any(
                    event.payload.get("content") == "independent input during rollback"
                    for event in events
                )
        if mode == "rejection":
            await service(factory).review(
                owner(),
                operation.id,
                MemoryReviewOutcome.UNTRUE,
                ceiling=Sensitivity.INTERNAL,
                key="erase",
            )
        else:
            await service(factory).delete(
                owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="erase"
            )
        async with factory() as uow:
            assert [await uow.memories.get(row.id, principal()) for row in originals] == originals
            events = await uow.events.list_after(session().id, 0, principal())
            assert any(event.event_type == "user.message.created" for event in events)

    async with factory() as uow:
        events = await uow.events.list_after(session().id, 0, principal())
        assert all(citation not in event.model_dump_json() for event in events)
        assert (await uow.runs.get(run().id, principal())).cancel_requested_at is not None
    current = await planner.current(session().id)
    assert current is not None and citation not in current.memory_snapshot
    rebuilt = await planner.plan(session(), agent(), principal(), model)
    assert rebuilt.epoch > frozen.epoch and citation not in rebuilt.memory_snapshot
    with pytest.raises(RunCancelledError, match="snapshot"):
        await planner._append(
            frozen.model_copy(update={"epoch": rebuilt.epoch + 1}),
            "context.epoch.rotated",
            "stale-derived-snapshot",
        )
