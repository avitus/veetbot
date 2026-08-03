from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from tests.contract.support import memory_stack


async def test_unit_of_work_factory_returns_fresh_boundaries_over_shared_state() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        agents=InMemoryAgentRepository(),
        sessions=sessions,
        runs=runs,
        events=events,
        invocations=InMemoryToolInvocationRepository(runs),
        clock=clock,
    )
    assert factory() is not factory()
    assert not factory.is_open()
    async with factory():
        assert factory.is_open()
    assert not factory.is_open()
