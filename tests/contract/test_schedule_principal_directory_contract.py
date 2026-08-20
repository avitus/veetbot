"""Shared current-authority directory contract for scheduled execution."""

from agent_core.adapters.identity import (
    ConfiguredSchedulePrincipalDirectory,
    StaticSchedulePrincipalDirectory,
)
from agent_core.ports.schedules import SchedulePrincipalDirectory
from tests.contract.support import principal


async def assert_schedule_principal_directory_returns_current_authority(
    directory: SchedulePrincipalDirectory,
) -> None:
    expected = principal()
    snapshot = await directory.current(expected.tenant_id, expected.principal_id)
    assert snapshot is not None
    assert snapshot.principal == expected
    assert snapshot.enabled
    assert snapshot.authority_version
    assert await directory.current(expected.tenant_id, "missing") is None


async def test_static_schedule_principal_directory_satisfies_contract() -> None:
    await assert_schedule_principal_directory_returns_current_authority(
        StaticSchedulePrincipalDirectory(principal())
    )


async def test_configured_schedule_principal_directory_satisfies_contract() -> None:
    await assert_schedule_principal_directory_returns_current_authority(
        ConfiguredSchedulePrincipalDirectory(principal())
    )
