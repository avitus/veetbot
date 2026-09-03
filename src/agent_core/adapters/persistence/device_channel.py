"""In-memory and PostgreSQL device-channel persistence adapters."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.mappers import (
    device_ingest_receipt_to_domain,
    device_ingest_receipt_values,
    device_invocation_to_domain,
    device_invocation_values,
    device_triage_mapping_to_domain,
    device_triage_mapping_values,
)
from agent_core.adapters.persistence.sqlalchemy_models import (
    DeviceIngestReceiptRow,
    DeviceInvocationRow,
    DeviceTriageSessionRow,
)
from agent_core.domain.devices import (
    TERMINAL_DEVICE_INVOCATION_STATUSES,
    DeviceIngestReceipt,
    DeviceInvocation,
    DeviceInvocationStatus,
    DeviceTriageMapping,
)
from agent_core.domain.errors import ConflictError, NotFoundError

type _ReceiptKey = tuple[UUID, str, str]
type _ChannelKey = tuple[UUID, str]


def _terminal(status: DeviceInvocationStatus) -> DeviceInvocationStatus:
    if status not in TERMINAL_DEVICE_INVOCATION_STATUSES:
        raise ValueError("device invocation result must be terminal")
    return status


def _already_expired() -> ConflictError:
    return ConflictError(
        "device invocation already expired",
        reason="device_invocation_expired",
    )


def _utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


class InMemoryDeviceInvocationStore:
    def __init__(self) -> None:
        self._rows: dict[UUID, DeviceInvocation] = {}

    async def create(self, invocation: DeviceInvocation) -> DeviceInvocation | None:
        if invocation.id in self._rows:
            return None
        self._rows[invocation.id] = invocation.model_copy(deep=True)
        return invocation.model_copy(deep=True)

    async def get(self, invocation_id: UUID) -> DeviceInvocation:
        row = self._rows.get(invocation_id)
        if row is None:
            raise NotFoundError("device invocation not found")
        return row.model_copy(deep=True)

    async def list_pending_for_device(
        self,
        device_id: UUID,
        *,
        now: datetime,
    ) -> list[DeviceInvocation]:
        rows = [
            row
            for row in self._rows.values()
            if row.device_id == device_id
            and row.status is DeviceInvocationStatus.PENDING
            and row.created_at <= now
        ]
        rows.sort(key=lambda row: (row.created_at, row.id))
        return [row.model_copy(deep=True) for row in rows]

    async def record_result(
        self,
        invocation_id: UUID,
        *,
        device_id: UUID,
        status: DeviceInvocationStatus,
        at: datetime,
    ) -> DeviceInvocation:
        posted = _terminal(status)
        row = self._rows.get(invocation_id)
        if row is None or row.device_id != device_id:
            raise NotFoundError("device invocation not found")
        if row.status is DeviceInvocationStatus.EXPIRED:
            raise _already_expired()
        if row.status is not DeviceInvocationStatus.PENDING:
            return row.model_copy(deep=True)
        resolved = row.model_copy(update={"status": posted, "resolved_at": at})
        self._rows[invocation_id] = resolved
        return resolved.model_copy(deep=True)

    async def expire_overdue(self, *, now: datetime, timeout_seconds: int) -> int:
        cutoff = now - timedelta(seconds=timeout_seconds)
        expired = 0
        for invocation_id, row in list(self._rows.items()):
            if row.status is not DeviceInvocationStatus.PENDING or row.created_at > cutoff:
                continue
            self._rows[invocation_id] = row.model_copy(
                update={"status": DeviceInvocationStatus.EXPIRED, "resolved_at": now}
            )
            expired += 1
        return expired


class InMemoryDeviceIngestStore:
    def __init__(self) -> None:
        self._receipts: dict[_ReceiptKey, DeviceIngestReceipt] = {}
        self._mappings: dict[_ChannelKey, DeviceTriageMapping] = {}

    async def record(self, receipt: DeviceIngestReceipt) -> DeviceIngestReceipt | None:
        key = (receipt.device_id, receipt.channel, receipt.digest)
        if key in self._receipts:
            return None
        self._receipts[key] = receipt.model_copy(deep=True)
        return receipt.model_copy(deep=True)

    async def get(
        self,
        device_id: UUID,
        channel: str,
        digest: str,
    ) -> DeviceIngestReceipt | None:
        stored = self._receipts.get((device_id, channel, digest))
        return None if stored is None else stored.model_copy(deep=True)

    async def attach_routing(
        self,
        *,
        device_id: UUID,
        channel: str,
        digest: str,
        session_id: UUID,
        run_id: UUID,
    ) -> None:
        key = (device_id, channel, digest)
        stored = self._receipts.get(key)
        if stored is None:
            raise NotFoundError("device ingest receipt not found")
        self._receipts[key] = stored.model_copy(update={"session_id": session_id, "run_id": run_id})

    async def count_for_utc_day(self, device_id: UUID, channel: str, *, day: date) -> int:
        start, end = _utc_day_bounds(day)
        return sum(
            1
            for receipt in self._receipts.values()
            if receipt.device_id == device_id
            and receipt.channel == channel
            and start <= receipt.accepted_at < end
        )

    async def get_triage_mapping(
        self,
        device_id: UUID,
        channel: str,
    ) -> DeviceTriageMapping | None:
        mapping = self._mappings.get((device_id, channel))
        return None if mapping is None else mapping.model_copy(deep=True)

    async def set_triage_mapping(self, mapping: DeviceTriageMapping) -> None:
        self._mappings[(mapping.device_id, mapping.channel)] = mapping.model_copy(deep=True)


class PostgresDeviceInvocationStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, invocation: DeviceInvocation) -> DeviceInvocation | None:
        inserted = (
            await self._session.scalars(
                pg_insert(DeviceInvocationRow)
                .values(**device_invocation_values(invocation))
                .on_conflict_do_nothing(index_elements=[DeviceInvocationRow.id])
                .returning(DeviceInvocationRow)
            )
        ).one_or_none()
        return None if inserted is None else device_invocation_to_domain(inserted)

    async def get(self, invocation_id: UUID) -> DeviceInvocation:
        row = (
            await self._session.scalars(
                select(DeviceInvocationRow).where(DeviceInvocationRow.id == invocation_id)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("device invocation not found")
        return device_invocation_to_domain(row)

    async def list_pending_for_device(
        self,
        device_id: UUID,
        *,
        now: datetime,
    ) -> list[DeviceInvocation]:
        rows = (
            await self._session.scalars(
                select(DeviceInvocationRow)
                .where(
                    DeviceInvocationRow.device_id == device_id,
                    DeviceInvocationRow.status == DeviceInvocationStatus.PENDING.value,
                    DeviceInvocationRow.created_at <= now,
                )
                .order_by(DeviceInvocationRow.created_at, DeviceInvocationRow.id)
            )
        ).all()
        return [device_invocation_to_domain(row) for row in rows]

    async def record_result(
        self,
        invocation_id: UUID,
        *,
        device_id: UUID,
        status: DeviceInvocationStatus,
        at: datetime,
    ) -> DeviceInvocation:
        posted = _terminal(status)
        row = (
            await self._session.scalars(
                select(DeviceInvocationRow)
                .where(DeviceInvocationRow.id == invocation_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None or row.device_id != device_id:
            raise NotFoundError("device invocation not found")
        recorded = device_invocation_to_domain(row)
        if recorded.status is DeviceInvocationStatus.EXPIRED:
            raise _already_expired()
        if recorded.status is not DeviceInvocationStatus.PENDING:
            return recorded
        await self._session.execute(
            update(DeviceInvocationRow)
            .where(DeviceInvocationRow.id == invocation_id)
            .values(status=posted.value, resolved_at=at)
        )
        return recorded.model_copy(update={"status": posted, "resolved_at": at})

    async def expire_overdue(self, *, now: datetime, timeout_seconds: int) -> int:
        cutoff = now - timedelta(seconds=timeout_seconds)
        expired = (
            await self._session.scalars(
                update(DeviceInvocationRow)
                .where(
                    DeviceInvocationRow.status == DeviceInvocationStatus.PENDING.value,
                    DeviceInvocationRow.created_at <= cutoff,
                )
                .values(status=DeviceInvocationStatus.EXPIRED.value, resolved_at=now)
                .returning(DeviceInvocationRow.id)
            )
        ).all()
        return len(expired)


class PostgresDeviceIngestStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, receipt: DeviceIngestReceipt) -> DeviceIngestReceipt | None:
        inserted = (
            await self._session.scalars(
                pg_insert(DeviceIngestReceiptRow)
                .values(**device_ingest_receipt_values(receipt))
                .on_conflict_do_nothing(
                    index_elements=[
                        DeviceIngestReceiptRow.device_id,
                        DeviceIngestReceiptRow.channel,
                        DeviceIngestReceiptRow.digest,
                    ]
                )
                .returning(DeviceIngestReceiptRow)
            )
        ).one_or_none()
        return None if inserted is None else device_ingest_receipt_to_domain(inserted)

    async def get(
        self,
        device_id: UUID,
        channel: str,
        digest: str,
    ) -> DeviceIngestReceipt | None:
        row = (
            await self._session.scalars(
                select(DeviceIngestReceiptRow).where(
                    DeviceIngestReceiptRow.device_id == device_id,
                    DeviceIngestReceiptRow.channel == channel,
                    DeviceIngestReceiptRow.digest == digest,
                )
            )
        ).one_or_none()
        return None if row is None else device_ingest_receipt_to_domain(row)

    async def attach_routing(
        self,
        *,
        device_id: UUID,
        channel: str,
        digest: str,
        session_id: UUID,
        run_id: UUID,
    ) -> None:
        routed = (
            await self._session.execute(
                update(DeviceIngestReceiptRow)
                .where(
                    DeviceIngestReceiptRow.device_id == device_id,
                    DeviceIngestReceiptRow.channel == channel,
                    DeviceIngestReceiptRow.digest == digest,
                )
                .values(session_id=session_id, run_id=run_id)
                .returning(DeviceIngestReceiptRow.digest)
            )
        ).scalar_one_or_none()
        if routed is None:
            raise NotFoundError("device ingest receipt not found")

    async def count_for_utc_day(self, device_id: UUID, channel: str, *, day: date) -> int:
        start, end = _utc_day_bounds(day)
        counted = await self._session.scalar(
            select(func.count())
            .select_from(DeviceIngestReceiptRow)
            .where(
                DeviceIngestReceiptRow.device_id == device_id,
                DeviceIngestReceiptRow.channel == channel,
                DeviceIngestReceiptRow.accepted_at >= start,
                DeviceIngestReceiptRow.accepted_at < end,
            )
        )
        return int(counted or 0)

    async def get_triage_mapping(
        self,
        device_id: UUID,
        channel: str,
    ) -> DeviceTriageMapping | None:
        row = (
            await self._session.scalars(
                select(DeviceTriageSessionRow).where(
                    DeviceTriageSessionRow.device_id == device_id,
                    DeviceTriageSessionRow.channel == channel,
                )
            )
        ).one_or_none()
        return None if row is None else device_triage_mapping_to_domain(row)

    async def set_triage_mapping(self, mapping: DeviceTriageMapping) -> None:
        values = device_triage_mapping_values(mapping)
        await self._session.execute(
            pg_insert(DeviceTriageSessionRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    DeviceTriageSessionRow.device_id,
                    DeviceTriageSessionRow.channel,
                ],
                set_={
                    "tenant_id": values["tenant_id"],
                    "session_id": values["session_id"],
                },
            )
        )
