"""Browser-authentication control-plane contract.

The isolated service's direct ceremony semantics are asserted where that service is
built, in ``test_hosted_profile_session_service_contract``. This module binds the
adapter a deployment without a profile service composes: every operation fails
closed with a stable, non-retryable provider error and never reports a ceremony.
"""

from uuid import UUID

import pytest

from agent_core.adapters.browser.unavailable import (
    UnavailableBrowserAuthenticationControlPlane,
)
from agent_core.domain.browser import BrowserAuthenticationMode, BrowserProviderError
from agent_core.ports.browser_sessions import BrowserAuthenticationControlPlane
from tests.contract.support import principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000d1")
CEREMONY_ID = UUID("00000000-0000-0000-0000-0000000000d2")


@pytest.mark.parametrize("mode", list(BrowserAuthenticationMode))
async def test_unconfigured_authentication_control_plane_fails_closed(
    mode: BrowserAuthenticationMode,
) -> None:
    control_plane: BrowserAuthenticationControlPlane = (
        UnavailableBrowserAuthenticationControlPlane()
    )
    operations = (
        control_plane.begin_authentication(
            PROFILE_ID,
            principal(),
            "provider-ref",
            login_url="https://example.org/login",
            mode=mode,
        ),
        control_plane.authentication_status(CEREMONY_ID, principal()),
        control_plane.cancel_authentication(CEREMONY_ID, principal()),
    )

    for operation in operations:
        with pytest.raises(BrowserProviderError) as refused:
            await operation
        assert refused.value.reason_code == "tool.browser.provider_unavailable"
        assert refused.value.retryable is False
