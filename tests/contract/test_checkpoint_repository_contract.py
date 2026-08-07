from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryCheckpointRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.domain.context import WorkingState
from agent_core.domain.events import NewEvent
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from tests.contract.support import NOW, RUN_ID, memory_stack, principal, run


async def test_checkpoint_repository_versions_and_restores_latest() -> None:
    repository = InMemoryCheckpointRepository()
    first = RunCheckpoint(
        run_id=RUN_ID,
        version=1,
        status=RunStatus.RUNNING,
        working_state={"step": 1},
        created_at=NOW,
    )
    await repository.write(RUN_ID, first, full=True)
    second = first.model_copy(update={"version": 2, "working_state": {"step": 2}}, deep=True)
    await repository.write(RUN_ID, second, full=False)
    assert await repository.latest(RUN_ID) == second
    assert await repository.prune(RUN_ID, terminal=False) == 0
    third = second.model_copy(update={"version": 3, "working_state": {"step": 3}}, deep=True)
    await repository.write(RUN_ID, third, full=True)
    fourth = third.model_copy(update={"version": 4, "working_state": {"step": 4}}, deep=True)
    await repository.write(RUN_ID, fourth, full=False)
    assert await repository.prune(RUN_ID, terminal=False) == 2
    terminal = fourth.model_copy(update={"version": 5, "status": RunStatus.COMPLETED}, deep=True)
    await repository.write(RUN_ID, terminal, full=True)
    assert await repository.prune(RUN_ID, terminal=True) == 2
    assert await repository.latest(RUN_ID) == terminal


async def test_checkpoint_seed_carries_state_at_the_inclusive_cutoff() -> None:
    clock, sessions, runs, events = await memory_stack()
    active_run = run()
    await runs.create(active_run)
    state = WorkingState(objective="state at cutoff")
    state_event = await events.append(
        NewEvent(
            session_id=active_run.session_id,
            run_id=active_run.id,
            event_type="context.working_state.updated",
            actor_type="contract",
            payload={"working_state": state.model_dump(mode="json")},
        )
    )
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )

    async with factory() as uow:
        checkpoint = await DurableCheckpointSeeder(clock)(
            uow,
            active_run,
            state_event.sequence,
            None,
            principal(),
        )

    assert WorkingState.model_validate(checkpoint.working_state["context"]) == state
