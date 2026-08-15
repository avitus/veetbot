from uuid import UUID

import pytest

from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import RunStatus
from tests.contract.support import RUN_ID, SESSION_ID, memory_stack, principal, run, session


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
