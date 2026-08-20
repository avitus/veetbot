"""In-memory standing browser grant repository."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserGrant
from agent_core.domain.errors import ConflictError, NotFoundError


class InMemoryBrowserGrantRepository:
    def __init__(self) -> None:
        self._grants: dict[UUID, BrowserGrant] = {}

    async def create(self, grant: BrowserGrant) -> BrowserGrant:
        if grant.id in self._grants:
            raise ConflictError("browser grant already exists")
        self._grants[grant.id] = grant.model_copy(deep=True)
        return grant.model_copy(deep=True)

    async def get(self, grant_id: UUID, principal: Principal) -> BrowserGrant:
        grant = self._grants.get(grant_id)
        if grant is None or not _owned(grant, principal):
            raise NotFoundError("browser grant not found")
        return grant.model_copy(deep=True)

    async def list(
        self,
        principal: Principal,
        *,
        profile_id: UUID | None = None,
        limit: int | None = None,
        after_created_at: datetime | None = None,
        after_id: UUID | None = None,
    ) -> list[BrowserGrant]:
        if (after_created_at is None) != (after_id is None):
            raise ValueError("pagination cursor components must be provided together")
        grants = [
            grant.model_copy(deep=True)
            for grant in self._grants.values()
            if _owned(grant, principal) and (profile_id is None or grant.profile_id == profile_id)
        ]
        ordered = sorted(grants, key=lambda grant: (grant.created_at, str(grant.id)))
        if after_created_at is not None and after_id is not None:
            ordered = [
                grant
                for grant in ordered
                if (grant.created_at, str(grant.id)) > (after_created_at, str(after_id))
            ]
        return ordered if limit is None else ordered[:limit]

    async def revoke(
        self,
        grant_id: UUID,
        principal: Principal,
        *,
        revoked_at: datetime,
    ) -> BrowserGrant:
        grant = await self.get(grant_id, principal)
        if grant.revoked_at is not None:
            return grant
        updated = BrowserGrant.model_validate(
            grant.model_dump() | {"revoked_at": revoked_at, "updated_at": revoked_at}
        )
        self._grants[grant_id] = updated
        return updated.model_copy(deep=True)

    async def delete(self, grant_id: UUID, principal: Principal) -> None:
        grant = self._grants.get(grant_id)
        if grant is None or not _owned(grant, principal):
            return
        del self._grants[grant_id]


def _owned(grant: BrowserGrant, principal: Principal) -> bool:
    return grant.tenant_id == principal.tenant_id and grant.principal_id == principal.principal_id
