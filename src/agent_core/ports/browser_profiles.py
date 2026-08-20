"""Trusted control-plane ports for persistent browser profiles."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserProfile,
    BrowserProfileProvisioning,
    BrowserProfileStatus,
)


class BrowserProfileRepository(Protocol):
    async def create(self, profile: BrowserProfile) -> BrowserProfile: ...

    async def get(self, profile_id: UUID, principal: Principal) -> BrowserProfile: ...

    def authentication_admission(
        self,
        profile_id: UUID,
        principal: Principal,
    ) -> AbstractAsyncContextManager[BrowserProfile]:
        """Lock one owned profile until authentication admission finishes."""
        ...

    async def list(
        self,
        principal: Principal,
        *,
        limit: int | None = None,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[BrowserProfile]:
        """Return ascending ``(created_at, id)`` rows after one strict composite cursor.

        Both cursor components must be supplied together.
        """
        ...

    async def bind(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
        provisioning: BrowserProfileProvisioning,
        updated_at: datetime,
    ) -> BrowserProfile:
        """Bind a reservation to a tenant-unique opaque provider reference."""
        ...

    async def transition(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
        status: BrowserProfileStatus,
        updated_at: datetime,
    ) -> BrowserProfile: ...

    async def delete(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
    ) -> None: ...


class BrowserProfileControlPlane(Protocol):
    async def provision(
        self,
        profile_id: UUID,
        principal: Principal,
        allowed_origins: tuple[str, ...],
    ) -> BrowserProfileProvisioning: ...

    async def revoke(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> None: ...

    async def delete(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> None: ...
