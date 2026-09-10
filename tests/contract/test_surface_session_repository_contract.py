"""Convention entry point for the shared surface session contract."""

from tests.contract.test_surface_repository_contract import (
    assert_surface_repositories_contract,
    in_memory_surface_repositories,
)


async def test_in_memory_surface_session_repository_contract() -> None:
    await assert_surface_repositories_contract(in_memory_surface_repositories())
