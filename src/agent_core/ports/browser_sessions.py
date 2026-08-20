"""Trusted orchestration ports for hosted browser sessions and login."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserAuthenticationView,
    BrowserLease,
    BrowserObservation,
)


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

    async def navigate(self, lease_ref: str, url: str) -> BrowserObservation: ...

    async def observe(self, lease_ref: str) -> BrowserObservation: ...

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
    ) -> BrowserObservation: ...

    async def close(self, lease_ref: str) -> None: ...


class BrowserAuthenticationControlPlane(Protocol):
    async def begin_authentication(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        login_url: str,
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
