from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from tests.contract.support import NOW, agent, memory_stack


async def test_repository_unit_of_work_exposes_one_repository_set() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        agents=InMemoryAgentRepository(),
        sessions=sessions,
        runs=runs,
        events=events,
        invocations=InMemoryToolInvocationRepository(runs),
    )
    async with factory() as uow:
        configured = agent()
        await uow.agents.put(configured)
        assert await uow.agents.latest_version(configured.id) == configured
    assert clock.now() == NOW
