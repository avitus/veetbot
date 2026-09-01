"""PostgreSQL persona document and nomination store (Milestone 22)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.functions import func

from agent_core.adapters.persistence.sqlalchemy_models import (
    PersonaDocumentRow,
    PersonaNominationRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import BeliefType, MemoryAuthority, Sensitivity
from agent_core.domain.persona import (
    PersonaDocument,
    PersonaEntry,
    PersonaEntrySource,
    PersonaNomination,
    PersonaNominationState,
)


def _entry_to_json(entry: PersonaEntry) -> dict[str, Any]:
    return {
        "text": entry.text,
        "source": entry.source.value,
        "source_belief_id": (
            str(entry.source_belief_id) if entry.source_belief_id is not None else None
        ),
        "sensitivity": entry.sensitivity.value,
    }


def _entry_from_json(value: dict[str, Any]) -> PersonaEntry:
    raw_belief = value.get("source_belief_id")
    return PersonaEntry(
        text=value["text"],
        source=PersonaEntrySource(value["source"]),
        source_belief_id=UUID(raw_belief) if raw_belief else None,
        sensitivity=Sensitivity(value["sensitivity"]),
    )


def _document_from_row(row: PersonaDocumentRow) -> PersonaDocument:
    return PersonaDocument(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        version=row.version,
        entries=tuple(_entry_from_json(entry) for entry in row.entries),
        source=PersonaEntrySource(row.source),
        source_nomination_id=row.source_nomination_id,
        created_at=row.created_at,
    )


def _nomination_from_row(row: PersonaNominationRow) -> PersonaNomination:
    return PersonaNomination(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        belief_id=row.belief_id,
        statement=row.statement,
        belief_type=BeliefType(row.belief_type),
        authority=MemoryAuthority(row.authority),
        confidence=row.confidence,
        corroboration_count=row.corroboration_count,
        sensitivity=Sensitivity(row.sensitivity),
        state=PersonaNominationState(row.state),
        consolidation_run_id=row.consolidation_run_id,
        nominated_at=row.nominated_at,
        resolved_at=row.resolved_at,
        affirmed_version=row.affirmed_version,
    )


class PostgresPersonaStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active(self, principal: Principal) -> PersonaDocument | None:
        row = (
            await self._session.execute(
                select(PersonaDocumentRow)
                .where(
                    PersonaDocumentRow.tenant_id == principal.tenant_id,
                    PersonaDocumentRow.principal_id == principal.principal_id,
                )
                .order_by(PersonaDocumentRow.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _document_from_row(row) if row is not None else None

    async def history(self, principal: Principal, *, limit: int = 50) -> list[PersonaDocument]:
        rows = (
            (
                await self._session.execute(
                    select(PersonaDocumentRow)
                    .where(
                        PersonaDocumentRow.tenant_id == principal.tenant_id,
                        PersonaDocumentRow.principal_id == principal.principal_id,
                    )
                    .order_by(PersonaDocumentRow.version.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_document_from_row(row) for row in rows]

    async def append_version(
        self, document: PersonaDocument, *, expected_version: int
    ) -> PersonaDocument:
        head = (
            await self._session.execute(
                select(func.max(PersonaDocumentRow.version)).where(
                    PersonaDocumentRow.tenant_id == document.tenant_id,
                    PersonaDocumentRow.principal_id == document.principal_id,
                )
            )
        ).scalar_one()
        current = int(head) if head is not None else 0
        if expected_version != current:
            raise ConflictError(
                f"persona expected version {expected_version} but head is {current}"
            )
        if document.version != current + 1:
            raise ConflictError(
                f"persona version {document.version} does not follow head {current}"
            )
        try:
            await self._session.execute(
                pg_insert(PersonaDocumentRow).values(
                    tenant_id=document.tenant_id,
                    principal_id=document.principal_id,
                    version=document.version,
                    entries=[_entry_to_json(entry) for entry in document.entries],
                    source=document.source.value,
                    source_nomination_id=document.source_nomination_id,
                    created_at=document.created_at,
                )
            )
        except IntegrityError as error:
            raise ConflictError(
                f"persona version {document.version} was written concurrently"
            ) from error
        return document

    async def nominate(self, nomination: PersonaNomination) -> PersonaNomination:
        existing_rows = (
            (
                await self._session.execute(
                    select(PersonaNominationRow).where(
                        PersonaNominationRow.tenant_id == nomination.tenant_id,
                        PersonaNominationRow.principal_id == nomination.principal_id,
                        PersonaNominationRow.belief_id == nomination.belief_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in existing_rows:
            state = PersonaNominationState(row.state)
            if state is PersonaNominationState.NOMINATED:
                return _nomination_from_row(row)
            if state in (
                PersonaNominationState.DECLINED,
                PersonaNominationState.AFFIRMED,
            ):
                raise ConflictError(
                    f"belief {nomination.belief_id} has a durable {state} nomination"
                )
        try:
            await self._session.execute(
                pg_insert(PersonaNominationRow).values(
                    id=nomination.id,
                    tenant_id=nomination.tenant_id,
                    principal_id=nomination.principal_id,
                    belief_id=nomination.belief_id,
                    statement=nomination.statement,
                    belief_type=nomination.belief_type.value,
                    authority=nomination.authority.value,
                    confidence=nomination.confidence,
                    corroboration_count=nomination.corroboration_count,
                    sensitivity=nomination.sensitivity.value,
                    state=nomination.state.value,
                    consolidation_run_id=nomination.consolidation_run_id,
                    nominated_at=nomination.nominated_at,
                    resolved_at=nomination.resolved_at,
                    affirmed_version=nomination.affirmed_version,
                )
            )
        except IntegrityError as error:
            raise ConflictError(
                f"belief {nomination.belief_id} was nominated concurrently"
            ) from error
        return nomination

    async def get_nomination(self, nomination_id: UUID, principal: Principal) -> PersonaNomination:
        return _nomination_from_row(await self._owned_row(nomination_id, principal))

    async def list_nominations(
        self,
        principal: Principal,
        *,
        state: PersonaNominationState | None = None,
    ) -> list[PersonaNomination]:
        query = select(PersonaNominationRow).where(
            PersonaNominationRow.tenant_id == principal.tenant_id,
            PersonaNominationRow.principal_id == principal.principal_id,
        )
        if state is not None:
            query = query.where(PersonaNominationRow.state == state.value)
        rows = (
            (
                await self._session.execute(
                    query.order_by(
                        PersonaNominationRow.nominated_at.desc(),
                        PersonaNominationRow.id.asc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_nomination_from_row(row) for row in rows]

    async def resolve_nomination(
        self,
        nomination_id: UUID,
        principal: Principal,
        *,
        state: PersonaNominationState,
        resolved_at: datetime,
        affirmed_version: int | None = None,
    ) -> PersonaNomination:
        row = await self._owned_row(nomination_id, principal)
        if PersonaNominationState(row.state) is not PersonaNominationState.NOMINATED:
            raise ConflictError(f"persona nomination {nomination_id} is already {row.state}")
        await self._session.execute(
            update(PersonaNominationRow)
            .where(PersonaNominationRow.id == nomination_id)
            .values(
                state=state.value,
                resolved_at=resolved_at,
                affirmed_version=affirmed_version,
            )
        )
        resolved = await self._owned_row(nomination_id, principal)
        return _nomination_from_row(resolved)

    async def _owned_row(self, nomination_id: UUID, principal: Principal) -> PersonaNominationRow:
        row = (
            await self._session.execute(
                select(PersonaNominationRow).where(
                    PersonaNominationRow.id == nomination_id,
                    PersonaNominationRow.tenant_id == principal.tenant_id,
                    PersonaNominationRow.principal_id == principal.principal_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"persona nomination {nomination_id} not found")
        return row
