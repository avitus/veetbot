"""In-memory and PostgreSQL delegation-ledger repositories."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.mappers import delegation_to_domain, delegation_values
from agent_core.adapters.persistence.sqlalchemy_models import DelegationRow
from agent_core.domain.agents import Principal
from agent_core.domain.delegations import Delegation, DelegationStatus
from agent_core.domain.errors import ConflictError, NotFoundError

_LIVE_STATUSES = (DelegationStatus.PENDING, DelegationStatus.RUNNING)


def _live_children(delegation: Delegation) -> int:
    if delegation.status not in _LIVE_STATUSES:
        return 0
    return sum(
        1
        for child in delegation.children
        if child.status is None and child.child_run_id is not None
    )


def _owned_by(delegation: Delegation, principal: Principal) -> bool:
    return (
        delegation.tenant_id == principal.tenant_id
        and delegation.principal_id == principal.principal_id
    )


class InMemoryDelegationRepository:
    def __init__(self) -> None:
        self._rows: dict[UUID, Delegation] = {}

    async def create(self, delegation: Delegation) -> Delegation:
        if delegation.id in self._rows:
            raise ConflictError("delegation already exists")
        if any(row.invocation_id == delegation.invocation_id for row in self._rows.values()):
            raise ConflictError("delegation invocation already recorded")
        self._rows[delegation.id] = delegation.model_copy(deep=True)
        return delegation.model_copy(deep=True)

    async def get(self, delegation_id: UUID, principal: Principal) -> Delegation:
        row = self._rows.get(delegation_id)
        if row is None or not _owned_by(row, principal):
            raise NotFoundError("delegation not found")
        return row.model_copy(deep=True)

    async def get_by_invocation(self, invocation_id: UUID) -> Delegation | None:
        row = next(
            (value for value in self._rows.values() if value.invocation_id == invocation_id),
            None,
        )
        return None if row is None else row.model_copy(deep=True)

    async def get_for_parent_run(self, parent_run_id: UUID) -> list[Delegation]:
        rows = [value for value in self._rows.values() if value.parent_run_id == parent_run_id]
        rows.sort(key=lambda value: (value.created_at, value.id))
        return [value.model_copy(deep=True) for value in rows]

    async def live_children_for_parent(self, parent_run_id: UUID) -> int:
        return sum(
            _live_children(value)
            for value in self._rows.values()
            if value.parent_run_id == parent_run_id
        )

    async def live_children_for_tenant(self, tenant_id: str) -> int:
        return sum(
            _live_children(value) for value in self._rows.values() if value.tenant_id == tenant_id
        )

    async def transition(
        self,
        delegation_id: UUID,
        expected: DelegationStatus,
        updated: Delegation,
    ) -> Delegation:
        row = self._rows.get(delegation_id)
        if row is None:
            raise NotFoundError("delegation not found")
        if updated.id != delegation_id:
            raise ConflictError("delegation transition must keep its identity")
        if row.status is not expected:
            raise ConflictError(
                "delegation status changed",
                reason="delegation_status_conflict",
            )
        self._rows[delegation_id] = updated.model_copy(deep=True)
        return updated.model_copy(deep=True)


class PostgresDelegationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, delegation: Delegation) -> Delegation:
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(DelegationRow).values(**delegation_values(delegation))
                )
        except IntegrityError as exc:
            raise ConflictError("delegation already exists") from exc
        return delegation

    async def get(self, delegation_id: UUID, principal: Principal) -> Delegation:
        row = (
            await self._session.scalars(
                select(DelegationRow).where(
                    DelegationRow.id == delegation_id,
                    DelegationRow.tenant_id == principal.tenant_id,
                    DelegationRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("delegation not found")
        return delegation_to_domain(row)

    async def get_by_invocation(self, invocation_id: UUID) -> Delegation | None:
        row = (
            await self._session.scalars(
                select(DelegationRow).where(DelegationRow.invocation_id == invocation_id)
            )
        ).one_or_none()
        return None if row is None else delegation_to_domain(row)

    async def get_for_parent_run(self, parent_run_id: UUID) -> list[Delegation]:
        rows = (
            await self._session.scalars(
                select(DelegationRow)
                .where(DelegationRow.parent_run_id == parent_run_id)
                .order_by(DelegationRow.created_at, DelegationRow.id)
            )
        ).all()
        return [delegation_to_domain(row) for row in rows]

    async def live_children_for_parent(self, parent_run_id: UUID) -> int:
        rows = (
            await self._session.scalars(
                select(DelegationRow).where(
                    DelegationRow.parent_run_id == parent_run_id,
                    DelegationRow.status.in_([status.value for status in _LIVE_STATUSES]),
                )
            )
        ).all()
        return sum(_live_children(delegation_to_domain(row)) for row in rows)

    async def live_children_for_tenant(self, tenant_id: str) -> int:
        rows = (
            await self._session.scalars(
                select(DelegationRow).where(
                    DelegationRow.tenant_id == tenant_id,
                    DelegationRow.status.in_([status.value for status in _LIVE_STATUSES]),
                )
            )
        ).all()
        return sum(_live_children(delegation_to_domain(row)) for row in rows)

    async def transition(
        self,
        delegation_id: UUID,
        expected: DelegationStatus,
        updated: Delegation,
    ) -> Delegation:
        if updated.id != delegation_id:
            raise ConflictError("delegation transition must keep its identity")
        values = delegation_values(updated)
        values.pop("id")
        written = (
            await self._session.execute(
                update(DelegationRow)
                .where(
                    DelegationRow.id == delegation_id,
                    DelegationRow.status == expected.value,
                )
                .values(**values)
                .returning(DelegationRow.id)
            )
        ).scalar_one_or_none()
        if written is not None:
            return updated
        exists = (
            await self._session.scalars(
                select(DelegationRow.id).where(DelegationRow.id == delegation_id)
            )
        ).one_or_none()
        if exists is None:
            raise NotFoundError("delegation not found")
        raise ConflictError(
            "delegation status changed",
            reason="delegation_status_conflict",
        )
