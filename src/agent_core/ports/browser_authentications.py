"""Secret-free authentication ceremony persistence port."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAuthenticationRecord,
    BrowserAuthenticationStatus,
)


class BrowserAuthenticationRepository(Protocol):
    async def create(
        self, authentication: BrowserAuthenticationRecord
    ) -> BrowserAuthenticationRecord: ...

    async def get(
        self, authentication_id: UUID, principal: Principal
    ) -> BrowserAuthenticationRecord: ...

    async def list(
        self,
        principal: Principal,
        *,
        profile_id: UUID | None = None,
    ) -> list[BrowserAuthenticationRecord]: ...

    async def transition(
        self,
        authentication_id: UUID,
        principal: Principal,
        *,
        expected_status: BrowserAuthenticationStatus,
        status: BrowserAuthenticationStatus,
        updated_at: datetime,
    ) -> BrowserAuthenticationRecord: ...
