"""Owner-scoped job records with immutable revisions and private grants."""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import JobRecordRow
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.jobs import JobRecord


def _conflict() -> ConflictError:
    return ConflictError(
        "The saved job changed; read it again before editing.", reason="job_changed"
    )


def _next(record: JobRecord, expected_version: int | None) -> None:
    if record.version != (expected_version or 0) + 1:
        raise _conflict()


class InMemoryJobStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], JobRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, principal: Principal, slug: str) -> JobRecord:
        async with self._lock:
            value = self._records.get((principal.tenant_id, principal.principal_id, slug))
            if value is None:
                raise NotFoundError("Job not found.")
            return value.model_copy(deep=True)

    async def list(self, principal: Principal) -> list[JobRecord]:
        async with self._lock:
            return [
                value.model_copy(deep=True)
                for key, value in sorted(self._records.items())
                if key[:2] == (principal.tenant_id, principal.principal_id)
            ]

    async def put(self, record: JobRecord, *, expected_version: int | None) -> JobRecord:
        _next(record, expected_version)
        key = (record.tenant_id, record.principal_id, record.slug)
        async with self._lock:
            previous = self._records.get(key)
            if (None if previous is None else previous.version) != expected_version:
                raise _conflict()
            self._records[key] = record.model_copy(deep=True)
            return record.model_copy(deep=True)

    async def erase_principal(self, principal: Principal) -> int:
        async with self._lock:
            keys = [
                key
                for key in self._records
                if key[:2] == (principal.tenant_id, principal.principal_id)
            ]
            for key in keys:
                del self._records[key]
            return len(keys)


class PostgresJobStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, principal: Principal, slug: str) -> JobRecord:
        body = (
            await self._session.execute(
                select(JobRecordRow.body).where(
                    JobRecordRow.tenant_id == principal.tenant_id,
                    JobRecordRow.principal_id == principal.principal_id,
                    JobRecordRow.slug == slug,
                )
            )
        ).scalar_one_or_none()
        if body is None:
            raise NotFoundError("Job not found.")
        return JobRecord.model_validate(body)

    async def list(self, principal: Principal) -> list[JobRecord]:
        bodies = (
            await self._session.execute(
                select(JobRecordRow.body)
                .where(
                    JobRecordRow.tenant_id == principal.tenant_id,
                    JobRecordRow.principal_id == principal.principal_id,
                )
                .order_by(JobRecordRow.slug)
            )
        ).scalars()
        return [JobRecord.model_validate(body) for body in bodies]

    async def put(self, record: JobRecord, *, expected_version: int | None) -> JobRecord:
        _next(record, expected_version)
        if expected_version is None:
            try:
                async with self._session.begin_nested():
                    await self._session.execute(
                        insert(JobRecordRow).values(
                            tenant_id=record.tenant_id,
                            principal_id=record.principal_id,
                            slug=record.slug,
                            version=record.version,
                            body=record.model_dump(mode="json"),
                        )
                    )
            except IntegrityError as exc:
                raise _conflict() from exc
        else:
            changed = (
                await self._session.execute(
                    update(JobRecordRow)
                    .where(
                        JobRecordRow.tenant_id == record.tenant_id,
                        JobRecordRow.principal_id == record.principal_id,
                        JobRecordRow.slug == record.slug,
                        JobRecordRow.version == expected_version,
                    )
                    .values(version=record.version, body=record.model_dump(mode="json"))
                    .returning(JobRecordRow.slug)
                )
            ).scalar_one_or_none()
            if changed is None:
                raise _conflict()
        return record.model_copy(deep=True)

    async def erase_principal(self, principal: Principal) -> int:
        deleted = (
            await self._session.execute(
                delete(JobRecordRow)
                .where(
                    JobRecordRow.tenant_id == principal.tenant_id,
                    JobRecordRow.principal_id == principal.principal_id,
                )
                .returning(JobRecordRow.slug)
            )
        ).all()
        return len(deleted)
