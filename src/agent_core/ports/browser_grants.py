"""Persistence port for tenant-scoped standing browser grants."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserGrant


class BrowserGrantRepository(Protocol):
    async def create(self, grant: BrowserGrant) -> BrowserGrant: ...

    async def get(self, grant_id: UUID, principal: Principal) -> BrowserGrant: ...

    async def list(
        self,
        principal: Principal,
        *,
        profile_id: UUID | None = None,
        limit: int | None = None,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[BrowserGrant]:
        """Return ascending ``(created_at, id)`` rows after one strict composite cursor.

        Both cursor components must be supplied together.
        """
        ...

    async def revoke(
        self,
        grant_id: UUID,
        principal: Principal,
        *,
        revoked_at: datetime,
    ) -> BrowserGrant: ...

    async def delete(self, grant_id: UUID, principal: Principal) -> None: ...
