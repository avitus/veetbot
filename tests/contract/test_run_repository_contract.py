import pytest

from agent_core.domain.errors import ConflictError
from agent_core.domain.runs import RunStatus
from tests.contract.support import RUN_ID, memory_stack, principal, run


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
