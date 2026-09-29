import pytest

from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.bootstrap import _memory_uow_repositories
from tests.contract.support import NOW, agent, memory_stack


async def test_repository_unit_of_work_exposes_one_repository_set() -> None:
    clock, sessions, runs, events = await memory_stack()
    agents = InMemoryAgentRepository()
    invocations = InMemoryToolInvocationRepository(runs)
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=agents,
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=invocations,
            clock=clock,
        )
    )
    async with factory() as uow:
        assert uow.browser_profiles is not None
        configured = agent()
        await uow.agents.put(configured)
        assert await uow.agents.latest_version(configured.id) == configured
    assert clock.now() == NOW


async def _factory() -> MemoryUnitOfWorkFactory:
    clock, sessions, runs, events = await memory_stack()
    return MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )


async def test_successive_units_of_work_see_one_committed_state() -> None:
    factory = await _factory()
    configured = agent()
    async with factory() as writer:
        await writer.agents.put(configured)
    async with factory() as reader:
        assert await reader.agents.latest_version(configured.id) == configured


async def test_a_failed_unit_of_work_runs_its_rollback_callbacks_newest_first() -> None:
    factory = await _factory()
    undone: list[str] = []

    async def undo(name: str) -> None:
        undone.append(name)

    async def broken() -> None:
        undone.append("broken")
        raise RuntimeError("compensation failed")

    with pytest.raises(LookupError, match="boom"):
        async with factory() as uow:
            uow.on_rollback(lambda: undo("first"))
            uow.on_rollback(broken)
            uow.on_rollback(lambda: undo("last"))
            raise LookupError("boom")

    assert undone == ["last", "broken", "first"]
    assert not factory.is_open()


async def test_a_committed_unit_of_work_never_runs_its_rollback_callbacks() -> None:
    factory = await _factory()
    undone: list[str] = []

    async def undo() -> None:
        undone.append("undone")

    uow = factory()
    async with uow:
        uow.on_rollback(undo)
    with pytest.raises(LookupError):
        async with uow:
            raise LookupError("a later failure on the same object")

    assert undone == []
