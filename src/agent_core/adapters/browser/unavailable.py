"""Fail-closed profile control planes for unconfigured deployments."""

from __future__ import annotations

from agent_core.domain.browser import BrowserProfileControlPlaneError, BrowserProviderError


class UnavailableBrowserProfileControlPlane:
    async def provision(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise BrowserProfileControlPlaneError(
            "browser_profile.control_plane_unavailable",
            retryable=False,
        )

    async def revoke(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BrowserProfileControlPlaneError(
            "browser_profile.control_plane_unavailable",
            retryable=False,
        )

    async def delete(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise BrowserProfileControlPlaneError(
            "browser_profile.control_plane_unavailable",
            retryable=False,
        )


class UnavailableBrowserAuthenticationControlPlane:
    async def begin_authentication(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise BrowserProviderError("tool.browser.provider_unavailable", retryable=False)

    async def authentication_status(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise BrowserProviderError("tool.browser.provider_unavailable", retryable=False)

    async def cancel_authentication(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise BrowserProviderError("tool.browser.provider_unavailable", retryable=False)
