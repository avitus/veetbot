"""Trusted orchestration ports for hosted browser sessions and login."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserAuthenticationMode,
    BrowserAuthenticationView,
    BrowserLease,
    BrowserObservation,
    BrowserSnapshot,
)

# ADR-0129: a page from the isolated service. A newer service returns the
# observation with element facts beside it; an older one, the observation.
type BrowserSessionPage = BrowserObservation | BrowserSnapshot


class BrowserSessionControlPlane(Protocol):
    async def acquire(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        run_id: UUID,
        attempt_number: int,
        deadline_at: datetime,
    ) -> BrowserLease: ...

    async def navigate(self, lease_ref: str, url: str) -> BrowserSessionPage: ...

    async def observe(self, lease_ref: str) -> BrowserSessionPage: ...

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
    ) -> BrowserSessionPage: ...

    async def renew(self, lease_ref: str, *, deadline_at: datetime) -> BrowserLease: ...

    async def close(self, lease_ref: str) -> None: ...


class BrowserAuthenticationControlPlane(Protocol):
    async def begin_authentication(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        login_url: str,
        mode: BrowserAuthenticationMode = BrowserAuthenticationMode.REMOTE,
    ) -> BrowserAuthenticationView: ...

    async def authentication_status(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView: ...

    async def cancel_authentication(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView: ...


class HostedBrowserControlPlane(
    BrowserSessionControlPlane,
    BrowserAuthenticationControlPlane,
    Protocol,
):
    async def refresh_authentication(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView: ...
