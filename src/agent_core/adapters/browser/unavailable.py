"""Fail-closed profile control planes for unconfigured deployments."""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAuthenticationView,
    BrowserProfileControlPlaneError,
    BrowserProfileProvisioning,
    BrowserProviderError,
)


class UnavailableBrowserProfileControlPlane:
    async def provision(
        self,
        profile_id: UUID,
        principal: Principal,
        allowed_origins: tuple[str, ...],
    ) -> BrowserProfileProvisioning:
        del profile_id, principal, allowed_origins
        raise BrowserProfileControlPlaneError(
            "browser_profile.control_plane_unavailable",
            retryable=False,
        )

    async def revoke(self, profile_id: UUID, principal: Principal, provider_ref: str) -> None:
        del profile_id, principal, provider_ref
        raise BrowserProfileControlPlaneError(
            "browser_profile.control_plane_unavailable",
            retryable=False,
        )

    async def delete(self, profile_id: UUID, principal: Principal, provider_ref: str) -> None:
        del profile_id, principal, provider_ref
        raise BrowserProfileControlPlaneError(
            "browser_profile.control_plane_unavailable",
            retryable=False,
        )


class UnavailableBrowserAuthenticationControlPlane:
    async def begin_authentication(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        login_url: str,
    ) -> BrowserAuthenticationView:
        del profile_id, principal, provider_ref, login_url
        raise BrowserProviderError("tool.browser.provider_unavailable", retryable=False)

    async def authentication_status(
        self, ceremony_id: UUID, principal: Principal
    ) -> BrowserAuthenticationView:
        del ceremony_id, principal
        raise BrowserProviderError("tool.browser.provider_unavailable", retryable=False)

    async def cancel_authentication(
        self, ceremony_id: UUID, principal: Principal
    ) -> BrowserAuthenticationView:
        del ceremony_id, principal
        raise BrowserProviderError("tool.browser.provider_unavailable", retryable=False)
