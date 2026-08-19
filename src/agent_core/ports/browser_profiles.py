"""Trusted control-plane ports for persistent browser profiles."""

from __future__ import annotations

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

    async def list(self, principal: Principal) -> list[BrowserProfile]: ...

    async def bind(
        self,
        profile_id: UUID,
        principal: Principal,
        *,
        expected_generation: int,
        provisioning: BrowserProfileProvisioning,
        updated_at: datetime,
    ) -> BrowserProfile: ...

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
