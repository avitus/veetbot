"""Principal-scoped email records over deterministic memory and PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.functions import func

from agent_core.adapters.persistence.mappers import email_record_to_domain, email_record_values
from agent_core.adapters.persistence.sqlalchemy_models import EmailRecordRow
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord
from agent_core.domain.errors import ConflictError


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("email page limit must be between 1 and 1000")


def _validate_revision(record: EmailRecord, expected_revision: int) -> None:
    if expected_revision < 0 or record.revision != expected_revision + 1:
        raise ConflictError("email record revision does not follow expected revision")


class InMemoryEmailStore:
    """Deterministic record semantics; transaction rollback belongs to PostgreSQL."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str, str], EmailRecord] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, principal: Principal) -> AsyncIterator[None]:
        key = (principal.tenant_id, principal.principal_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    async def get(self, principal: Principal, kind: str, key: str) -> EmailRecord | None:
        record = self._records.get((principal.tenant_id, principal.principal_id, kind, key))
        return None if record is None else record.model_copy(deep=True)

    async def list(
        self, principal: Principal, kind: str, *, after: str | None = None, limit: int = 1000
    ) -> list[EmailRecord]:
        _validate_limit(limit)
        selected = sorted(
            (
                record
                for record in self._records.values()
                if record.tenant_id == principal.tenant_id
                and record.principal_id == principal.principal_id
                and record.kind == kind
                and (after is None or record.key > after)
            ),
            key=lambda record: record.key,
        )
        return [record.model_copy(deep=True) for record in selected[:limit]]

    async def put(self, record: EmailRecord, *, expected_revision: int) -> EmailRecord:
        _validate_revision(record, expected_revision)
        key = (record.tenant_id, record.principal_id, record.kind, record.key)
        current = self._records.get(key)
        if (0 if current is None else current.revision) != expected_revision:
            raise ConflictError("email record revision changed")
        self._records[key] = record.model_copy(deep=True)
        return record.model_copy(deep=True)

    async def delete(
        self, principal: Principal, kind: str, key: str, *, expected_revision: int
    ) -> None:
        address = (principal.tenant_id, principal.principal_id, kind, key)
        current = self._records.get(address)
        if current is None or current.revision != expected_revision:
            raise ConflictError("email record revision changed or is absent")
        del self._records[address]


class PostgresEmailStore:
    """Atomic compare-and-swap records; caller owns transaction and RLS context."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def lock(self, principal: Principal) -> AsyncIterator[None]:
        address = f"email:{len(principal.tenant_id)}:{principal.tenant_id}:{principal.principal_id}"
        lock_id = int.from_bytes(hashlib.sha256(address.encode()).digest()[:8], signed=True)
        await self._session.execute(select(func.pg_advisory_xact_lock(lock_id)))
        yield

    async def get(self, principal: Principal, kind: str, key: str) -> EmailRecord | None:
        row = (
            await self._session.execute(
                select(EmailRecordRow)
                .where(
                    EmailRecordRow.tenant_id == principal.tenant_id,
                    EmailRecordRow.principal_id == principal.principal_id,
                    EmailRecordRow.kind == kind,
                    EmailRecordRow.key == key,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return None if row is None else email_record_to_domain(row)

    async def list(
        self, principal: Principal, kind: str, *, after: str | None = None, limit: int = 1000
    ) -> list[EmailRecord]:
        _validate_limit(limit)
        query = select(EmailRecordRow).where(
            EmailRecordRow.tenant_id == principal.tenant_id,
            EmailRecordRow.principal_id == principal.principal_id,
            EmailRecordRow.kind == kind,
        )
        if after is not None:
            query = query.where(EmailRecordRow.key > after)
        rows = (
            (
                await self._session.execute(
                    query.order_by(EmailRecordRow.key)
                    .limit(limit)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return [email_record_to_domain(row) for row in rows]

    async def put(self, record: EmailRecord, *, expected_revision: int) -> EmailRecord:
        _validate_revision(record, expected_revision)
        values = email_record_values(record)
        if expected_revision == 0:
            written = (
                await self._session.execute(
                    pg_insert(EmailRecordRow)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=["tenant_id", "principal_id", "kind", "key"]
                    )
                    .returning(EmailRecordRow.key)
                )
            ).scalar_one_or_none()
        else:
            written = (
                await self._session.execute(
                    update(EmailRecordRow)
                    .where(
                        EmailRecordRow.tenant_id == record.tenant_id,
                        EmailRecordRow.principal_id == record.principal_id,
                        EmailRecordRow.kind == record.kind,
                        EmailRecordRow.key == record.key,
                        EmailRecordRow.revision == expected_revision,
                    )
                    .values(**values)
                    .returning(EmailRecordRow.key)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
        if written is None:
            raise ConflictError("email record revision changed")
        return record.model_copy(deep=True)

    async def delete(
        self, principal: Principal, kind: str, key: str, *, expected_revision: int
    ) -> None:
        removed = (
            await self._session.execute(
                delete(EmailRecordRow)
                .where(
                    EmailRecordRow.tenant_id == principal.tenant_id,
                    EmailRecordRow.principal_id == principal.principal_id,
                    EmailRecordRow.kind == kind,
                    EmailRecordRow.key == key,
                    EmailRecordRow.revision == expected_revision,
                )
                .returning(EmailRecordRow.key)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
        if removed is None:
            raise ConflictError("email record revision changed or is absent")
