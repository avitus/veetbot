from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import RunStatus
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.support import RUN_ID, SESSION_ID, memory_stack, principal, run, session


async def higher_priority_work_contract(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    """Only this owner's due queued/running work can defer an import."""
    cases = [
        (RunStatus.QUEUED, 0, False, False, True),
        (RunStatus.RUNNING, 0, False, False, True),
        (RunStatus.WAITING_FOR_APPROVAL, 0, False, False, False),
        (RunStatus.WAITING_FOR_USER, 0, False, False, False),
        (RunStatus.COMPLETED, 0, False, False, False),
        (RunStatus.QUEUED, 10, False, False, False),
        (RunStatus.QUEUED, 20, False, False, False),
        (RunStatus.QUEUED, 0, True, False, False),
        (RunStatus.RUNNING, 0, False, True, False),
    ]
    for index, (status, priority, future, foreign, expected) in enumerate(cases):
        owner = principal().model_copy(update={"principal_id": f"owner-{index}"})
        source = session().model_copy(
            update={
                "id": UUID(int=5000 + index),
                "principal_id": "another-owner" if foreign else owner.principal_id,
            }
        )
        work = run(status=status).model_copy(
            update={
                "id": UUID(int=6000 + index),
                "session_id": source.id,
                "priority": priority,
                "scheduled_for": clock.now() + timedelta(seconds=30) if future else None,
            }
        )
        async with factory() as uow:
            await uow.sessions.create(source)
            await uow.runs.create(work)
        async with factory() as uow:
            assert await uow.runs.has_higher_priority_work(owner, 10) is expected


async def test_import_priority_checks_due_work_for_the_same_owner() -> None:
    from tests.contract.support import memory_uow_factory

    clock, factory = await memory_uow_factory()
    await higher_priority_work_contract(factory, clock)


async def test_run_repository_guards_state_transitions() -> None:
    _clock, _sessions, repository, _events = await memory_stack()
    await repository.create(run())
    running = await repository.transition(RUN_ID, RunStatus.QUEUED, RunStatus.RUNNING)
    assert running.status is RunStatus.RUNNING
    with pytest.raises(ConflictError):
        await repository.transition(RUN_ID, RunStatus.QUEUED, RunStatus.COMPLETED)
    assert (await repository.get(RUN_ID, principal())).status is RunStatus.RUNNING

    mutated = running.model_copy(update={"tenant_id": "another-tenant", "step_count": 1})
    with pytest.raises(ConflictError, match="only counters"):
        await repository.update_counters(mutated)


async def test_run_repository_atomically_rejects_a_second_active_run_for_a_session() -> None:
    _clock, _sessions, repository, _events = await memory_stack()
    await repository.create(run())

    with pytest.raises(ConflictError, match="active run"):
        await repository.create(run().model_copy(update={"id": UUID(int=RUN_ID.int + 1)}))

    await repository.transition(RUN_ID, RunStatus.QUEUED, RunStatus.RUNNING)
    await repository.transition(RUN_ID, RunStatus.RUNNING, RunStatus.COMPLETED)
    replacement = run().model_copy(update={"id": UUID(int=RUN_ID.int + 2)})
    await repository.create(replacement)
    assert await repository.get(replacement.id, principal()) == replacement


async def test_latest_for_sessions_silently_filters_inaccessible_ids() -> None:
    _clock, sessions, repository, _events = await memory_stack()
    await repository.create(run())
    foreign_session_id = UUID(int=901)
    await sessions.create(
        session().model_copy(
            update={
                "id": foreign_session_id,
                "tenant_id": "another-tenant",
                "principal_id": "another-principal",
            }
        )
    )

    latest = await repository.latest_for_sessions(
        [SESSION_ID, UUID(int=902), foreign_session_id], principal()
    )

    assert latest == {SESSION_ID: run()}
