"""Principal-scoped email records over deterministic memory and PostgreSQL."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, and_, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.functions import func

from agent_core.adapters.persistence.mappers import email_record_to_domain, email_record_values
from agent_core.adapters.persistence.people_reference_index import reference_overlap
from agent_core.adapters.persistence.sqlalchemy_models import EmailRecordRow
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord
from agent_core.domain.erasure import erased_email_payload
from agent_core.domain.errors import ConflictError


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("email page limit must be between 1 and 1000")


def _validate_revision(record: EmailRecord, expected_revision: int) -> None:
    if expected_revision < 0 or record.revision != expected_revision + 1:
        raise ConflictError("email record revision does not follow expected revision")


def _validate_source_window(
    account_ids: Sequence[str],
    since: datetime,
    until: datetime,
    after: tuple[datetime, str] | None,
    limit: int,
) -> None:
    if (
        isinstance(limit, bool)
        or not 1 <= limit <= 100
        or not 1 <= len(account_ids) <= 10
        or len(account_ids) != len(set(account_ids))
        or since.tzinfo is None
        or until.tzinfo is None
        or since >= until
        or (after is not None and (after[0].tzinfo is None or not after[1]))
    ):
        raise ValueError("email import source window is invalid")


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

    async def list_semantic_window(
        self,
        principal: Principal,
        *,
        account_ids: Sequence[str],
        since: datetime,
        until: datetime,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> builtins.list[EmailRecord]:
        _validate_source_window(account_ids, since, until, after, limit)
        selected = []
        for row in self._records.values():
            if (
                row.tenant_id != principal.tenant_id
                or row.principal_id != principal.principal_id
                or row.kind != "semantic_source"
                or row.payload.get("excluded")
                or row.payload.get("account_id") not in account_ids
            ):
                continue
            raw = row.payload.get("evidence_at")
            if not isinstance(raw, str):
                continue
            at = datetime.fromisoformat(raw)
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError("invalid retained email source")
            if since <= at < until and (after is None or (at, row.key) > after):
                selected.append((at, row.key, row))
        selected.sort(key=lambda value: (value[0], value[1]))
        return [row.model_copy(deep=True) for _, _, row in selected[:limit]]

    async def list_tasks(
        self,
        principal: Principal,
        *,
        created_since: datetime | None = None,
        after: str | None = None,
        limit: int = 1000,
    ) -> builtins.list[EmailRecord]:
        _validate_limit(limit)
        selected = sorted(
            (
                record
                for record in self._records.values()
                if record.tenant_id == principal.tenant_id
                and record.principal_id == principal.principal_id
                and record.kind == "task"
                and (after is None or record.key > after)
                and (
                    record.payload.get("settled_cost") is None
                    or (
                        created_since is not None
                        and datetime.fromisoformat(str(record.payload["created_at"]))
                        >= created_since
                    )
                )
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

    async def fence_people_erasure(
        self, principal: Principal, belief_ids: Sequence[UUID], erased_at: datetime
    ) -> int:
        ids = {str(key) for key in belief_ids}
        sources = [
            row
            for row in self._records.values()
            if (
                row.tenant_id == principal.tenant_id
                and row.principal_id == principal.principal_id
                and row.kind == "semantic_source"
                and isinstance(linked := row.payload.get("memory_ids"), list)
                and ids.intersection(str(key) for key in linked)
            )
        ]
        pairs = {
            (str(row.payload.get("account_id")), str(row.payload.get("provider_thread_id")))
            for row in sources
        }
        threads = [
            row
            for row in self._records.values()
            if (
                row.tenant_id == principal.tenant_id
                and row.principal_id == principal.principal_id
                and row.kind == "thread"
                and (str(row.payload.get("account_id")), str(row.payload.get("provider_thread_id")))
                in pairs
            )
        ]
        keys = {row.key for row in threads}
        assessments = [
            row
            for row in self._records.values()
            if (
                row.tenant_id == principal.tenant_id
                and row.principal_id == principal.principal_id
                and row.kind == "assessment"
                and row.key in keys
            )
        ]
        for row in sources + threads:
            await self.put(
                row.model_copy(
                    update={
                        "revision": row.revision + 1,
                        "updated_at": erased_at,
                        "payload": erased_email_payload(row.kind, row.payload),
                    }
                ),
                expected_revision=row.revision,
            )
        for row in assessments:
            await self.delete(principal, row.kind, row.key, expected_revision=row.revision)
        return len(sources) + len(threads) + len(assessments)

    async def purge_people_erasure(self, principal: Principal) -> bool:
        return False


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
                    or_(EmailRecordRow.kind != "assessment", ~EmailRecordRow.erasure_pending),
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
            or_(EmailRecordRow.kind != "assessment", ~EmailRecordRow.erasure_pending),
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

    async def list_semantic_window(
        self,
        principal: Principal,
        *,
        account_ids: Sequence[str],
        since: datetime,
        until: datetime,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> builtins.list[EmailRecord]:
        _validate_source_window(account_ids, since, until, after, limit)
        at = EmailRecordRow.payload["evidence_at"].astext.cast(DateTime(timezone=True))
        query = select(EmailRecordRow).where(
            EmailRecordRow.tenant_id == principal.tenant_id,
            EmailRecordRow.principal_id == principal.principal_id,
            EmailRecordRow.kind == "semantic_source",
            ~EmailRecordRow.erasure_pending,
            EmailRecordRow.payload["account_id"].astext.in_(account_ids),
            func.coalesce(EmailRecordRow.payload["excluded"].astext, "false") == "false",
            at >= since,
            at < until,
        )
        if after is not None:
            query = query.where(
                or_(at > after[0], and_(at == after[0], EmailRecordRow.key > after[1]))
            )
        try:
            rows = (
                await self._session.scalars(query.order_by(at, EmailRecordRow.key).limit(limit))
            ).all()
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) not in {"22007", "22008"}:
                raise
            raise ValueError("invalid retained email source") from exc
        return [email_record_to_domain(row) for row in rows]

    async def list_tasks(
        self,
        principal: Principal,
        *,
        created_since: datetime | None = None,
        after: str | None = None,
        limit: int = 1000,
    ) -> builtins.list[EmailRecord]:
        _validate_limit(limit)
        eligible = EmailRecordRow.payload["settled_cost"].astext.is_(None)
        if created_since is not None:
            eligible = or_(
                eligible,
                EmailRecordRow.payload["created_at"].astext.cast(DateTime(timezone=True))
                >= created_since,
            )
        query = select(EmailRecordRow).where(
            EmailRecordRow.tenant_id == principal.tenant_id,
            EmailRecordRow.principal_id == principal.principal_id,
            EmailRecordRow.kind == "task",
            eligible,
        )
        if after is not None:
            query = query.where(EmailRecordRow.key > after)
        rows = (
            await self._session.scalars(
                query.order_by(EmailRecordRow.key)
                .limit(limit)
                .execution_options(populate_existing=True)
            )
        ).all()
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
                        ~EmailRecordRow.erasure_pending,
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

    async def fence_people_erasure(
        self, principal: Principal, belief_ids: Sequence[UUID], erased_at: datetime
    ) -> int:
        if not belief_ids:
            return 0
        source = aliased(EmailRecordRow, name="people_source")
        supported = (
            select(source.key)
            .where(
                source.tenant_id == principal.tenant_id,
                source.principal_id == principal.principal_id,
                source.kind == "semantic_source",
                reference_overlap("(people_source.payload->'memory_ids')::text", list(belief_ids)),
                source.payload["account_id"].astext == EmailRecordRow.payload["account_id"].astext,
                source.payload["provider_thread_id"].astext
                == EmailRecordRow.payload["provider_thread_id"].astext,
            )
            .correlate(EmailRecordRow)
            .exists()
        )
        threads = select(EmailRecordRow.key).where(
            EmailRecordRow.tenant_id == principal.tenant_id,
            EmailRecordRow.principal_id == principal.principal_id,
            EmailRecordRow.kind == "thread",
            supported,
        )
        changed = await self._session.scalars(
            update(EmailRecordRow)
            .where(
                EmailRecordRow.tenant_id == principal.tenant_id,
                EmailRecordRow.principal_id == principal.principal_id,
                ~EmailRecordRow.erasure_pending,
                or_(
                    and_(
                        EmailRecordRow.kind == "semantic_source",
                        reference_overlap("(payload->'memory_ids')::text", list(belief_ids)),
                    ),
                    and_(
                        EmailRecordRow.kind.in_(["thread", "assessment"]),
                        EmailRecordRow.key.in_(threads),
                    ),
                ),
            )
            .values(
                erasure_pending=True, revision=EmailRecordRow.revision + 1, updated_at=erased_at
            )
            .returning(EmailRecordRow.key)
            .execution_options(synchronize_session=False)
        )
        return len(changed.all())

    async def purge_people_erasure(self, principal: Principal) -> bool:
        scope = (
            EmailRecordRow.tenant_id == principal.tenant_id,
            EmailRecordRow.principal_id == principal.principal_id,
            EmailRecordRow.erasure_pending,
        )
        rows = (
            await self._session.scalars(
                select(EmailRecordRow)
                .where(*scope)
                .order_by(EmailRecordRow.kind, EmailRecordRow.key)
                .limit(256)
                .execution_options(populate_existing=True)
            )
        ).all()
        for row in rows:
            if row.kind == "assessment":
                await self._session.delete(row)
            else:
                row.payload = erased_email_payload(row.kind, row.payload)
                row.erasure_pending = False
        await self._session.flush()
        return bool(
            await self._session.scalar(select(select(EmailRecordRow.key).where(*scope).exists()))
        )
