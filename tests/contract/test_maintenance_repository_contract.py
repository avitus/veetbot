from agent_core.adapters.persistence.memory import InMemoryMaintenanceRepository


async def test_maintenance_repository_returns_bounded_candidate_sets() -> None:
    repository = InMemoryMaintenanceRepository()
    assert await repository.live_run_leases() == frozenset()
    assert await repository.projection_sessions(10) == []
    assert await repository.trajectory_runs(10) == []
    assert await repository.checkpoint_runs(10) == []
