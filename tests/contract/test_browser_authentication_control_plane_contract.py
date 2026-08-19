"""Shared direct browser-authentication control-plane contract."""

from pathlib import Path

from agent_core.domain.browser import BrowserAuthenticationStatus
from tests.contract import test_hosted_profile_session_service_contract as service_contract


async def test_browser_authentication_control_plane_contract(tmp_path: Path) -> None:
    """Only the isolated runtime decides readiness and user intervention."""

    await service_contract.test_authentication_ceremony_is_direct_single_use_and_runtime_decided(
        tmp_path / "needs-user",
        BrowserAuthenticationStatus.NEEDS_USER,
        BrowserAuthenticationStatus.NEEDS_USER,
    )
    await service_contract.test_authentication_cancellation_is_scoped_idempotent_and_closes_runtime(
        tmp_path / "cancel"
    )
