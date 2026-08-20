"""In-memory secret-free authentication repository."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    ALLOWED_BROWSER_AUTHENTICATION_TRANSITIONS,
    BrowserAuthenticationRecord,
    BrowserAuthenticationStatus,
)
from agent_core.domain.errors import ConflictError, NotFoundError


class InMemoryBrowserAuthenticationRepository:
    def __init__(self) -> None:
        self._authentications: dict[UUID, BrowserAuthenticationRecord] = {}

    async def create(
        self, authentication: BrowserAuthenticationRecord
    ) -> BrowserAuthenticationRecord:
        if authentication.id in self._authentications:
            raise ConflictError("browser authentication already exists")
        self._authentications[authentication.id] = authentication.model_copy(deep=True)
        return authentication.model_copy(deep=True)

    async def get(
        self, authentication_id: UUID, principal: Principal
    ) -> BrowserAuthenticationRecord:
        authentication = self._authentications.get(authentication_id)
        if authentication is None or not _owned(authentication, principal):
            raise NotFoundError("browser authentication not found")
        return authentication.model_copy(deep=True)

    async def list(
        self,
        principal: Principal,
        *,
        profile_id: UUID | None = None,
    ) -> list[BrowserAuthenticationRecord]:
        records = [
            record.model_copy(deep=True)
            for record in self._authentications.values()
            if _owned(record, principal) and (profile_id is None or record.profile_id == profile_id)
        ]
        return sorted(records, key=lambda record: (record.created_at, str(record.id)))

    async def transition(
        self,
        authentication_id: UUID,
        principal: Principal,
        *,
        expected_status: BrowserAuthenticationStatus,
        status: BrowserAuthenticationStatus,
        updated_at: datetime,
    ) -> BrowserAuthenticationRecord:
        authentication = await self.get(authentication_id, principal)
        if authentication.status is not expected_status:
            raise ConflictError("browser authentication status changed")
        if updated_at < authentication.updated_at:
            raise ConflictError("browser authentication update time moved backwards")
        if authentication.status is status:
            if updated_at == authentication.updated_at:
                return authentication
            updated = authentication.model_copy(update={"updated_at": updated_at}, deep=True)
            self._authentications[authentication_id] = updated
            return updated.model_copy(deep=True)
        if status not in ALLOWED_BROWSER_AUTHENTICATION_TRANSITIONS[authentication.status]:
            raise ConflictError("browser authentication transition is not allowed")
        updated = authentication.model_copy(
            update={"status": status, "updated_at": updated_at},
            deep=True,
        )
        self._authentications[authentication_id] = updated
        return updated.model_copy(deep=True)


def _owned(authentication: BrowserAuthenticationRecord, principal: Principal) -> bool:
    return (
        authentication.tenant_id == principal.tenant_id
        and authentication.principal_id == principal.principal_id
    )
