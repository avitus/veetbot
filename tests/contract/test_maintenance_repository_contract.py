from datetime import timedelta
from uuid import UUID

from agent_core.adapters.persistence.memory import InMemoryMaintenanceRepository
from tests.contract.support import NOW, SESSION_ID, principal


async def test_maintenance_repository_returns_bounded_candidate_sets() -> None:
    repository = InMemoryMaintenanceRepository()
    assert await repository.live_run_leases() == frozenset()
    assert await repository.is_live_run_lease(UUID(int=1), 1) is False
    assert await repository.projection_sessions(10) == []
    assert await repository.trajectory_runs(10) == []
    assert await repository.checkpoint_runs(10) == []
    assert (
        await repository.pending_memory_sessions(
            principal(), idle_before=NOW + timedelta(seconds=30), limit=10
        )
        == []
    )


async def test_maintenance_repository_claims_one_consolidator_per_session() -> None:
    repository = InMemoryMaintenanceRepository()
    owner = principal()

    assert await repository.acquire_memory_session(owner, SESSION_ID)
    assert not await repository.acquire_memory_session(owner, SESSION_ID)

    await repository.release_memory_session(owner, SESSION_ID)
    assert await repository.acquire_memory_session(owner, SESSION_ID)
    await repository.release_memory_session(owner, SESSION_ID)
