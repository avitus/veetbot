from agent_core.adapters.persistence.memory import InMemoryCheckpointRepository
from agent_core.domain.runs import RunCheckpoint, RunStatus
from tests.contract.support import NOW, RUN_ID


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
