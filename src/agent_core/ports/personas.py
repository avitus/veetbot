"""Persona document and nomination store port (Milestone 22)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.persona import (
    PersonaDocument,
    PersonaNomination,
    PersonaNominationState,
)


class PersonaStore(Protocol):
    """Versioned persona documents and their nominations, principal-scoped.

    `append_version` is the only document write: it admits `document` only
    when `expected_version` names the current head (0 for an unwritten
    persona) and `document.version` is exactly the next version; anything
    else is a `ConflictError`. `nominate` is idempotent while a nomination
    for the same belief is open, and refuses a belief whose nomination was
    affirmed or declined — decline is durable. Cross-principal reads raise
    `NotFoundError`, indistinguishable from absence.
    """

    async def active(self, principal: Principal) -> PersonaDocument | None: ...

    async def history(self, principal: Principal, *, limit: int = 50) -> list[PersonaDocument]: ...

    async def append_version(
        self, document: PersonaDocument, *, expected_version: int
    ) -> PersonaDocument: ...

    async def nominate(self, nomination: PersonaNomination) -> PersonaNomination: ...

    async def get_nomination(
        self, nomination_id: UUID, principal: Principal
    ) -> PersonaNomination: ...

    async def list_nominations(
        self,
        principal: Principal,
        *,
        state: PersonaNominationState | None = None,
    ) -> list[PersonaNomination]: ...

    async def resolve_nomination(
        self,
        nomination_id: UUID,
        principal: Principal,
        *,
        state: PersonaNominationState,
        resolved_at: datetime,
        affirmed_version: int | None = None,
    ) -> PersonaNomination: ...
