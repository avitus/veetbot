"""Principal-scoped call records over deterministic memory and PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import DateTime, cast, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.functions import func

from agent_core.adapters.persistence.sqlalchemy_models import CallRecordRow
from agent_core.domain.agents import Principal
from agent_core.domain.calls import CallRecord
from agent_core.domain.errors import ConflictError


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("call page limit must be between 1 and 1000")


def _validate_revision(record: CallRecord, expected_revision: int) -> None:
    if expected_revision < 0 or record.revision != expected_revision + 1:
        raise ConflictError("call record revision does not follow expected revision")


def call_record_to_domain(row: CallRecordRow) -> CallRecord:
    return CallRecord(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        kind=row.kind,
        key=row.key,
        revision=row.revision,
        payload=row.payload,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def call_record_values(record: CallRecord) -> dict[str, object]:
    return record.model_dump()


class InMemoryCallStore:
    """Deterministic record semantics; transaction rollback belongs to PostgreSQL."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str, str], CallRecord] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(self, principal: Principal) -> AsyncIterator[None]:
        key = (principal.tenant_id, principal.principal_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    async def get(self, principal: Principal, kind: str, key: str) -> CallRecord | None:
        record = self._records.get((principal.tenant_id, principal.principal_id, kind, key))
        return None if record is None else record.model_copy(deep=True)

    async def list(
        self,
        principal: Principal,
        kind: str,
        *,
        after: str | None = None,
        limit: int = 100,
        active: bool | None = None,
        due_at: datetime | None = None,
    ) -> list[CallRecord]:
        _validate_limit(limit)
        selected = sorted(
            (
                record
                for record in self._records.values()
                if record.tenant_id == principal.tenant_id
                and record.principal_id == principal.principal_id
                and record.kind == kind
                and (after is None or record.key > after)
                and (active is None or record.payload.get("active") is active)
                and (
                    due_at is None
                    or "retry_at" not in record.payload
                    or datetime.fromisoformat(str(record.payload["retry_at"])) <= due_at
                )
            ),
            key=lambda record: record.key,
        )
        return [record.model_copy(deep=True) for record in selected[:limit]]

    async def put(self, record: CallRecord, *, expected_revision: int) -> CallRecord:
        _validate_revision(record, expected_revision)
        key = (record.tenant_id, record.principal_id, record.kind, record.key)
        current = self._records.get(key)
        if (0 if current is None else current.revision) != expected_revision:
            raise ConflictError("call record revision changed")
        self._records[key] = record.model_copy(deep=True)
        return record.model_copy(deep=True)


class PostgresCallStore:
    """Atomic compare-and-swap records; caller owns transaction and RLS context."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def lock(self, principal: Principal) -> AsyncIterator[None]:
        address = f"call:{len(principal.tenant_id)}:{principal.tenant_id}:{principal.principal_id}"
        lock_id = int.from_bytes(hashlib.sha256(address.encode()).digest()[:8], signed=True)
        await self._session.execute(select(func.pg_advisory_xact_lock(lock_id)))
        yield

    async def get(self, principal: Principal, kind: str, key: str) -> CallRecord | None:
        row = (
            await self._session.execute(
                select(CallRecordRow)
                .where(
                    CallRecordRow.tenant_id == principal.tenant_id,
                    CallRecordRow.principal_id == principal.principal_id,
                    CallRecordRow.kind == kind,
                    CallRecordRow.key == key,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return None if row is None else call_record_to_domain(row)

    async def list(
        self,
        principal: Principal,
        kind: str,
        *,
        after: str | None = None,
        limit: int = 100,
        active: bool | None = None,
        due_at: datetime | None = None,
    ) -> list[CallRecord]:
        _validate_limit(limit)
        query = select(CallRecordRow).where(
            CallRecordRow.tenant_id == principal.tenant_id,
            CallRecordRow.principal_id == principal.principal_id,
            CallRecordRow.kind == kind,
        )
        if active is not None:
            query = query.where(CallRecordRow.payload["active"].as_boolean() == active)
        if due_at is not None:
            retry = CallRecordRow.payload["retry_at"].as_string()
            query = query.where(
                or_(retry.is_(None), cast(retry, DateTime(timezone=True)) <= due_at)
            )
        if after is not None:
            query = query.where(CallRecordRow.key > after)
        rows = (
            (
                await self._session.execute(
                    query.order_by(CallRecordRow.key)
                    .limit(limit)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        return [call_record_to_domain(row) for row in rows]

    async def put(self, record: CallRecord, *, expected_revision: int) -> CallRecord:
        _validate_revision(record, expected_revision)
        values = call_record_values(record)
        if expected_revision == 0:
            written = (
                await self._session.execute(
                    pg_insert(CallRecordRow)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=["tenant_id", "principal_id", "kind", "key"]
                    )
                    .returning(CallRecordRow.key)
                )
            ).scalar_one_or_none()
        else:
            written = (
                await self._session.execute(
                    update(CallRecordRow)
                    .where(
                        CallRecordRow.tenant_id == record.tenant_id,
                        CallRecordRow.principal_id == record.principal_id,
                        CallRecordRow.kind == record.kind,
                        CallRecordRow.key == record.key,
                        CallRecordRow.revision == expected_revision,
                    )
                    .values(**values)
                    .returning(CallRecordRow.key)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
        if written is None:
            raise ConflictError("call record revision changed")
        return record.model_copy(deep=True)
