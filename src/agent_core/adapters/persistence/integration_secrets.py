"""Sealed integration keys in memory and PostgreSQL; writes serialize per owner."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import cast
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import IntegrationSecretRow
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.integration_secrets import AuthScheme, IntegrationSecret


def _owner(principal: Principal, record: IntegrationSecret) -> None:
    if (record.tenant_id, record.principal_id) != (principal.tenant_id, principal.principal_id):
        raise NotFoundError("Integration key not found.")


class InMemoryIntegrationSecretStore:
    def __init__(self) -> None:
        self._rows: dict[UUID, IntegrationSecret] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def locked(self, principal: Principal) -> AsyncIterator[None]:
        async with self._lock:
            snapshot = self._rows.copy()
            try:
                yield
            except BaseException:
                self._rows = snapshot
                raise

    async def get(self, principal: Principal, key_id: UUID) -> IntegrationSecret | None:
        row = self._rows.get(key_id)
        return (
            row
            if row
            and (row.tenant_id, row.principal_id) == (principal.tenant_id, principal.principal_id)
            else None
        )

    async def list(self, principal: Principal) -> list[IntegrationSecret]:
        return sorted(
            [
                row
                for row in self._rows.values()
                if row.deleted_at is None
                and (row.tenant_id, row.principal_id)
                == (principal.tenant_id, principal.principal_id)
            ],
            key=lambda row: str(row.id),
        )

    async def put(self, principal: Principal, record: IntegrationSecret) -> None:
        _owner(principal, record)
        if record.id in self._rows:
            _owner(principal, self._rows[record.id])
        self._rows[record.id] = record


class PostgresIntegrationSecretStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def locked(self, principal: Principal) -> AsyncIterator[None]:
        key = json.dumps(["integration-secrets", principal.tenant_id, principal.principal_id])
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
        )
        yield

    async def get(self, principal: Principal, key_id: UUID) -> IntegrationSecret | None:
        row = (
            await self._session.execute(
                select(IntegrationSecretRow).where(
                    IntegrationSecretRow.id == key_id,
                    IntegrationSecretRow.tenant_id == principal.tenant_id,
                    IntegrationSecretRow.principal_id == principal.principal_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._record(row)

    async def list(self, principal: Principal) -> list[IntegrationSecret]:
        rows = (
            await self._session.execute(
                select(IntegrationSecretRow)
                .where(
                    IntegrationSecretRow.tenant_id == principal.tenant_id,
                    IntegrationSecretRow.principal_id == principal.principal_id,
                    IntegrationSecretRow.deleted_at.is_(None),
                )
                .order_by(IntegrationSecretRow.id)
            )
        ).scalars()
        return [self._record(row) for row in rows]

    async def put(self, principal: Principal, record: IntegrationSecret) -> None:
        _owner(principal, record)
        statement = insert(IntegrationSecretRow).values(**asdict(record))
        result = await self._session.execute(
            statement.on_conflict_do_nothing(index_elements=["id"]).returning(
                IntegrationSecretRow.id
            )
        )
        if result.scalar_one_or_none() is not None:
            return
        result = await self._session.execute(
            update(IntegrationSecretRow)
            .where(
                IntegrationSecretRow.id == record.id,
                IntegrationSecretRow.tenant_id == principal.tenant_id,
                IntegrationSecretRow.principal_id == principal.principal_id,
            )
            .values(**asdict(record))
            .returning(IntegrationSecretRow.id)
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundError("Integration key not found.")

    @staticmethod
    def _record(row: IntegrationSecretRow) -> IntegrationSecret:
        return IntegrationSecret(
            row.tenant_id,
            row.principal_id,
            row.id,
            row.label,
            row.origin,
            cast(AuthScheme, row.auth_scheme),
            row.revision,
            row.created_at,
            row.updated_at,
            row.sealed_value,
            row.deleted_at,
        )
