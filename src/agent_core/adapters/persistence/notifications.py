"""In-memory and PostgreSQL device and notification persistence adapters."""

from __future__ import annotations

import asyncio
import builtins
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import String, and_, cast, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from agent_core.adapters.notification_wakeup import NOTIFICATION_WAKEUP_CHANNEL
from agent_core.adapters.persistence.mappers import (
    device_registration_idempotency_to_domain,
    device_registration_idempotency_values,
    device_to_domain,
    device_values,
    new_notification_values,
    notification_delivery_to_domain,
    notification_delivery_values,
    notification_to_domain,
)
from agent_core.adapters.persistence.sqlalchemy_models import (
    DeviceRegistrationIdempotencyRow,
    DeviceRow,
    NotificationDeliveryRow,
    NotificationOutboxRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.devices import (
    Device,
    DeviceCursor,
    DeviceRegistrationIdempotencyRecord,
    DeviceStatus,
    PushEnvironment,
    PushProvider,
    PushTarget,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.notifications import (
    TEST_NOTIFICATION_DEDUPE_PREFIX,
    DeliveryOutcome,
    NewNotification,
    Notification,
    NotificationCursor,
    NotificationDelivery,
    NotificationKind,
    NotificationStatus,
    test_notification_target_device_id,
)
from agent_core.ports.determinism import Clock


class InMemoryDeviceRegistry:
    def __init__(self) -> None:
        self._devices: dict[UUID, Device] = {}

    async def upsert(self, device: Device, principal: Principal) -> Device:
        if not _owned_by(device, principal):
            raise NotFoundError("device not found")
        existing = next(
            (
                value
                for value in self._devices.values()
                if _owned_by(value, principal) and value.client_device_id == device.client_device_id
            ),
            None,
        )
        stored = device
        if existing is not None:
            stored = device.model_copy(
                update={"id": existing.id, "created_at": existing.created_at}
            )
        elif device.id in self._devices:
            raise ConflictError("device identifier already exists")
        self._move_token(stored, principal)
        self._devices[stored.id] = stored
        return stored.model_copy(deep=True)

    async def get(self, device_id: UUID, principal: Principal) -> Device:
        device = self._devices.get(device_id)
        if device is None or not _owned_by(device, principal):
            raise NotFoundError("device not found")
        return device.model_copy(deep=True)

    async def get_by_client_device_id(
        self, client_device_id: str, principal: Principal
    ) -> Device | None:
        device = next(
            (
                value
                for value in self._devices.values()
                if _owned_by(value, principal) and value.client_device_id == client_device_id
            ),
            None,
        )
        return None if device is None else device.model_copy(deep=True)

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: DeviceCursor | None = None,
    ) -> list[Device]:
        _positive_limit(limit, "device")
        devices = [value for value in self._devices.values() if _owned_by(value, principal)]
        devices.sort(key=lambda value: (value.created_at, value.id), reverse=True)
        if cursor is not None:
            devices = [
                value
                for value in devices
                if (value.created_at, value.id) < (cursor.created_at, cursor.id)
            ]
        return [value.model_copy(deep=True) for value in devices[:limit]]

    async def revoke(self, device_id: UUID, principal: Principal, at: datetime) -> Device:
        instant = _aware_utc(at)
        device = await self.get(device_id, principal)
        revoked = device.model_copy(
            update={
                "push_provider": None,
                "push_token": None,
                "push_environment": None,
                "status": DeviceStatus.REVOKED,
                "revoked_at": instant,
                "updated_at": instant,
            }
        )
        revoked = Device.model_validate(revoked.model_dump())
        self._devices[device_id] = revoked
        return revoked.model_copy(deep=True)

    async def delete(self, device_id: UUID, principal: Principal) -> None:
        await self.get(device_id, principal)
        del self._devices[device_id]

    async def invalidate_push_token(
        self, device_id: UUID, reason: str, at: datetime
    ) -> Device | None:
        if not reason.strip():
            raise ValueError("push-token invalidation requires a reason")
        device = self._devices.get(device_id)
        if device is None or device.push_token is None:
            return None
        instant = _aware_utc(at)
        invalidated = device.model_copy(
            update={
                "push_provider": None,
                "push_token": None,
                "push_environment": None,
                "push_token_invalidated_at": instant,
                "updated_at": instant,
            }
        )
        invalidated = Device.model_validate(invalidated.model_dump())
        self._devices[device_id] = invalidated
        return invalidated.model_copy(deep=True)

    async def push_targets(
        self,
        tenant_id: str,
        principal_id: str,
        kind: NotificationKind,
    ) -> builtins.list[PushTarget]:
        targets: builtins.list[PushTarget] = []
        for device in self._devices.values():
            if (
                device.tenant_id != tenant_id
                or device.principal_id != principal_id
                or device.status is not DeviceStatus.ACTIVE
                or device.push_provider is None
                or device.push_token is None
                or kind in device.muted_kinds
            ):
                continue
            targets.append(
                PushTarget(
                    device_id=device.id,
                    provider=device.push_provider,
                    token=device.push_token,
                    environment=device.push_environment,
                    app_bundle_id=device.app_bundle_id,
                )
            )
        targets.sort(key=lambda target: target.device_id)
        return targets

    def _move_token(self, device: Device, principal: Principal) -> None:
        if device.push_token is None or device.push_provider is None:
            return
        token = device.push_token.get_secret_value()
        for current in list(self._devices.values()):
            if (
                current.id == device.id
                or current.status is not DeviceStatus.ACTIVE
                or current.push_provider is not device.push_provider
                or current.push_token is None
                or current.push_token.get_secret_value() != token
            ):
                continue
            if not _owned_by(current, principal):
                raise ConflictError("push token already belongs to another principal")
            self._devices[current.id] = current.model_copy(
                update={
                    "push_provider": None,
                    "push_token": None,
                    "push_environment": None,
                    "updated_at": device.updated_at,
                }
            )
            self._devices[current.id] = Device.model_validate(
                self._devices[current.id].model_dump()
            )

    def _unscoped(self, device_id: UUID) -> Device | None:
        device = self._devices.get(device_id)
        return None if device is None else device.model_copy(deep=True)


class InMemoryDeviceRegistrationIdempotencyRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], DeviceRegistrationIdempotencyRecord] = {}

    async def get(
        self,
        tenant_id: str,
        principal_id: str,
        key: str,
    ) -> DeviceRegistrationIdempotencyRecord | None:
        record = self._records.get((tenant_id, principal_id, key))
        return None if record is None else record.model_copy(deep=True)

    async def create(
        self,
        record: DeviceRegistrationIdempotencyRecord,
    ) -> DeviceRegistrationIdempotencyRecord:
        identity = (record.tenant_id, record.principal_id, record.key)
        existing = self._records.get(identity)
        if existing is None:
            self._records[identity] = record.model_copy(deep=True)
            return record
        if existing == record:
            return existing.model_copy(deep=True)
        raise ConflictError("device registration idempotency key already exists")


class InMemoryNotificationOutbox:
    def __init__(self, clock: Clock, devices: InMemoryDeviceRegistry) -> None:
        self._clock = clock
        self._devices = devices
        self._notifications: dict[UUID, Notification] = {}
        self._dedupe_keys: set[str] = set()
        self._deliveries: dict[tuple[UUID, UUID, int], NotificationDelivery] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, notification: NewNotification) -> Notification | None:
        async with self._lock:
            if notification.dedupe_key in self._dedupe_keys:
                return None
            if notification.id in self._notifications:
                raise ConflictError("notification identifier already exists")
            stored = _pending_notification(notification)
            self._notifications[stored.id] = stored
            self._dedupe_keys.add(stored.dedupe_key)
            return stored.model_copy(deep=True)

    async def claim_due(
        self,
        now: datetime,
        limit: int,
        claimant: str,
        lease_seconds: float,
        providers: frozenset[PushProvider],
    ) -> list[Notification]:
        instant = _aware_utc(now)
        _positive_limit(limit, "notification claim")
        if not claimant.strip():
            raise ValueError("notification claimant cannot be blank")
        if lease_seconds <= 0:
            raise ValueError("notification lease must be positive")
        if not providers:
            raise ValueError("notification claim requires at least one provider")
        async with self._lock:
            due: list[Notification] = []
            for value in self._notifications.values():
                if (
                    value.status is not NotificationStatus.PENDING
                    or value.next_attempt_at > instant
                    or (value.claimed_until is not None and value.claimed_until > instant)
                ):
                    continue
                targets = await self._devices.push_targets(
                    value.tenant_id,
                    value.principal_id,
                    value.kind,
                )
                if value.kind is NotificationKind.TEST:
                    target_device_id = test_notification_target_device_id(value.dedupe_key)
                    targets = [target for target in targets if target.device_id == target_device_id]
                terminal_devices = {
                    delivery.device_id
                    for delivery in self._deliveries.values()
                    if delivery.notification_id == value.id
                    and delivery.outcome is not DeliveryOutcome.RETRY
                }
                pending_targets = [
                    target for target in targets if target.device_id not in terminal_devices
                ]
                if pending_targets and not any(
                    target.provider in providers for target in pending_targets
                ):
                    continue
                due.append(value)
            due.sort(key=lambda value: (value.next_attempt_at, value.created_at, value.id))
            claimed: list[Notification] = []
            for value in due[:limit]:
                updated = value.model_copy(
                    update={
                        "attempts": value.attempts + 1,
                        "claimed_by": claimant,
                        "claimed_until": instant + timedelta(seconds=lease_seconds),
                    }
                )
                self._notifications[value.id] = updated
                claimed.append(updated.model_copy(deep=True))
            return claimed

    async def list_pending_older_than(
        self,
        before: datetime,
        limit: int,
    ) -> list[Notification]:
        cutoff = _aware_utc(before)
        _positive_limit(limit, "notification backlog")
        async with self._lock:
            values = [
                value
                for value in self._notifications.values()
                if value.status is NotificationStatus.PENDING and value.created_at < cutoff
            ]
            values.sort(key=lambda value: (value.created_at, value.id))
            return [value.model_copy(deep=True) for value in values[:limit]]

    async def record_delivery(self, delivery: NotificationDelivery) -> None:
        async with self._lock:
            notification = self._notifications.get(delivery.notification_id)
            device = self._devices._unscoped(delivery.device_id)
            if (
                notification is None
                or device is None
                or device.tenant_id != notification.tenant_id
                or device.principal_id != notification.principal_id
            ):
                raise NotFoundError("notification delivery owner not found")
            key = (delivery.notification_id, delivery.device_id, delivery.attempt)
            if key in self._deliveries or any(
                value.id == delivery.id for value in self._deliveries.values()
            ):
                raise ConflictError("notification delivery already exists")
            self._deliveries[key] = delivery.model_copy(deep=True)

    async def list_deliveries(self, notification_id: UUID) -> list[NotificationDelivery]:
        async with self._lock:
            if notification_id not in self._notifications:
                raise NotFoundError("notification not found")
            values = [
                delivery.model_copy(deep=True)
                for delivery in self._deliveries.values()
                if delivery.notification_id == notification_id
            ]
            return sorted(values, key=lambda delivery: (delivery.attempt, delivery.device_id))

    async def list_deliveries_for(
        self,
        notification_ids: tuple[UUID, ...],
    ) -> dict[UUID, list[NotificationDelivery]]:
        async with self._lock:
            missing = set(notification_ids) - self._notifications.keys()
            if missing:
                raise NotFoundError("notification not found")
            grouped: dict[UUID, list[NotificationDelivery]] = {
                notification_id: [] for notification_id in notification_ids
            }
            for delivery in self._deliveries.values():
                if delivery.notification_id in grouped:
                    grouped[delivery.notification_id].append(delivery.model_copy(deep=True))
            for deliveries in grouped.values():
                deliveries.sort(key=lambda delivery: (delivery.attempt, delivery.device_id))
            return grouped

    async def settle(
        self,
        notification_id: UUID,
        attempt: int,
        status: NotificationStatus,
        next_attempt_at: datetime | None,
    ) -> bool:
        if attempt <= 0:
            raise ValueError("notification settlement attempt must be positive")
        async with self._lock:
            notification = self._notifications.get(notification_id)
            if notification is None:
                raise NotFoundError("notification not found")
            if (
                notification.status is not NotificationStatus.PENDING
                or notification.attempts != attempt
                or notification.claimed_by is None
            ):
                return False
            if status is NotificationStatus.PENDING:
                if next_attempt_at is None:
                    raise ValueError("pending notification requires next attempt")
                updated = notification.model_copy(
                    update={
                        "next_attempt_at": _aware_utc(next_attempt_at),
                        "claimed_by": None,
                        "claimed_until": None,
                    }
                )
            else:
                if next_attempt_at is not None:
                    raise ValueError("settled notification cannot have a next attempt")
                updated = notification.model_copy(
                    update={
                        "status": status,
                        "claimed_by": None,
                        "claimed_until": None,
                        "settled_at": _aware_utc(self._clock.now()),
                    }
                )
            self._notifications[notification_id] = updated
            return True

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: NotificationCursor | None = None,
    ) -> list[Notification]:
        _positive_limit(limit, "notification")
        async with self._lock:
            values = [
                value
                for value in self._notifications.values()
                if value.tenant_id == principal.tenant_id
                and value.principal_id == principal.principal_id
            ]
            values.sort(key=lambda value: (value.created_at, value.id), reverse=True)
            if cursor is not None:
                values = [
                    value
                    for value in values
                    if (value.created_at, value.id) < (cursor.created_at, cursor.id)
                ]
            return [value.model_copy(deep=True) for value in values[:limit]]


class PostgresDeviceRegistry:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, device: Device, principal: Principal) -> Device:
        if not _owned_by(device, principal):
            raise NotFoundError("device not found")
        existing = (
            await self._session.scalars(
                select(DeviceRow).where(
                    DeviceRow.tenant_id == principal.tenant_id,
                    DeviceRow.principal_id == principal.principal_id,
                    DeviceRow.client_device_id == device.client_device_id,
                )
            )
        ).one_or_none()
        stored = device
        if existing is not None:
            stored = device.model_copy(
                update={"id": existing.id, "created_at": existing.created_at}
            )
        values = device_values(stored)
        updates = {
            key: value
            for key, value in values.items()
            if key not in {"id", "tenant_id", "principal_id", "client_device_id", "created_at"}
        }
        try:
            async with self._session.begin_nested():
                if stored.push_provider is not None and stored.push_token is not None:
                    await self._session.execute(
                        update(DeviceRow)
                        .where(
                            DeviceRow.id != stored.id,
                            DeviceRow.tenant_id == principal.tenant_id,
                            DeviceRow.principal_id == principal.principal_id,
                            DeviceRow.status == DeviceStatus.ACTIVE.value,
                            DeviceRow.push_provider == stored.push_provider.value,
                            DeviceRow.push_token == stored.push_token.get_secret_value(),
                        )
                        .values(
                            push_provider=None,
                            push_token=None,
                            push_environment=None,
                            updated_at=stored.updated_at,
                        )
                    )
                statement = (
                    pg_insert(DeviceRow)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[
                            DeviceRow.tenant_id,
                            DeviceRow.principal_id,
                            DeviceRow.client_device_id,
                        ],
                        set_=updates,
                    )
                    .returning(DeviceRow)
                    .execution_options(populate_existing=True)
                )
                row = (await self._session.scalars(statement)).one()
        except IntegrityError:
            raise ConflictError("device identity or push token already exists") from None
        return device_to_domain(row)

    async def get(self, device_id: UUID, principal: Principal) -> Device:
        row = (
            await self._session.scalars(
                select(DeviceRow).where(
                    DeviceRow.id == device_id,
                    DeviceRow.tenant_id == principal.tenant_id,
                    DeviceRow.principal_id == principal.principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("device not found")
        return device_to_domain(row)

    async def get_by_client_device_id(
        self, client_device_id: str, principal: Principal
    ) -> Device | None:
        row = (
            await self._session.scalars(
                select(DeviceRow).where(
                    DeviceRow.tenant_id == principal.tenant_id,
                    DeviceRow.principal_id == principal.principal_id,
                    DeviceRow.client_device_id == client_device_id,
                )
            )
        ).one_or_none()
        return None if row is None else device_to_domain(row)

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: DeviceCursor | None = None,
    ) -> list[Device]:
        statement = select(DeviceRow).where(
            DeviceRow.tenant_id == principal.tenant_id,
            DeviceRow.principal_id == principal.principal_id,
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    DeviceRow.created_at < cursor.created_at,
                    and_(DeviceRow.created_at == cursor.created_at, DeviceRow.id < cursor.id),
                )
            )
        rows = (
            await self._session.scalars(
                statement.order_by(DeviceRow.created_at.desc(), DeviceRow.id.desc()).limit(
                    _positive_limit(limit, "device")
                )
            )
        ).all()
        return [device_to_domain(row) for row in rows]

    async def revoke(self, device_id: UUID, principal: Principal, at: datetime) -> Device:
        row = (
            await self._session.scalars(
                update(DeviceRow)
                .where(
                    DeviceRow.id == device_id,
                    DeviceRow.tenant_id == principal.tenant_id,
                    DeviceRow.principal_id == principal.principal_id,
                )
                .values(
                    push_provider=None,
                    push_token=None,
                    push_environment=None,
                    status=DeviceStatus.REVOKED.value,
                    revoked_at=_aware_utc(at),
                    updated_at=_aware_utc(at),
                )
                .returning(DeviceRow)
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("device not found")
        return device_to_domain(row)

    async def delete(self, device_id: UUID, principal: Principal) -> None:
        row = await self._session.scalar(
            delete(DeviceRow)
            .where(
                DeviceRow.id == device_id,
                DeviceRow.tenant_id == principal.tenant_id,
                DeviceRow.principal_id == principal.principal_id,
            )
            .returning(DeviceRow.id)
        )
        if row is None:
            raise NotFoundError("device not found")

    async def invalidate_push_token(
        self, device_id: UUID, reason: str, at: datetime
    ) -> Device | None:
        if not reason.strip():
            raise ValueError("push-token invalidation requires a reason")
        instant = _aware_utc(at)
        row = (
            await self._session.scalars(
                update(DeviceRow)
                .where(DeviceRow.id == device_id, DeviceRow.push_token.is_not(None))
                .values(
                    push_provider=None,
                    push_token=None,
                    push_environment=None,
                    push_token_invalidated_at=instant,
                    updated_at=instant,
                )
                .returning(DeviceRow)
            )
        ).one_or_none()
        return None if row is None else device_to_domain(row)

    async def push_targets(
        self,
        tenant_id: str,
        principal_id: str,
        kind: NotificationKind,
    ) -> builtins.list[PushTarget]:
        rows = (
            await self._session.scalars(
                select(DeviceRow)
                .where(
                    DeviceRow.tenant_id == tenant_id,
                    DeviceRow.principal_id == principal_id,
                    DeviceRow.status == DeviceStatus.ACTIVE.value,
                    DeviceRow.push_provider.is_not(None),
                    DeviceRow.push_token.is_not(None),
                    ~DeviceRow.muted_kinds.contains([kind.value]),
                )
                .order_by(DeviceRow.id)
            )
        ).all()
        targets: builtins.list[PushTarget] = []
        for row in rows:
            assert row.push_provider is not None and row.push_token is not None
            targets.append(
                PushTarget(
                    device_id=row.id,
                    provider=PushProvider(row.push_provider),
                    token=SecretStr(row.push_token),
                    environment=(
                        None
                        if row.push_environment is None
                        else PushEnvironment(row.push_environment)
                    ),
                    app_bundle_id=row.app_bundle_id,
                )
            )
        return targets


class PostgresDeviceRegistrationIdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        tenant_id: str,
        principal_id: str,
        key: str,
    ) -> DeviceRegistrationIdempotencyRecord | None:
        row = await self._session.get(
            DeviceRegistrationIdempotencyRow,
            (tenant_id, principal_id, key),
        )
        return None if row is None else device_registration_idempotency_to_domain(row)

    async def create(
        self,
        record: DeviceRegistrationIdempotencyRecord,
    ) -> DeviceRegistrationIdempotencyRecord:
        statement = (
            pg_insert(DeviceRegistrationIdempotencyRow)
            .values(**device_registration_idempotency_values(record))
            .on_conflict_do_nothing()
            .returning(DeviceRegistrationIdempotencyRow.key)
        )
        if await self._session.scalar(statement) is not None:
            return record
        existing = await self.get(record.tenant_id, record.principal_id, record.key)
        if existing == record:
            return existing
        raise ConflictError("device registration idempotency key already exists")


class PostgresNotificationOutbox:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock

    async def enqueue(self, notification: NewNotification) -> Notification | None:
        statement = (
            pg_insert(NotificationOutboxRow)
            .values(**new_notification_values(notification))
            .on_conflict_do_nothing(index_elements=[NotificationOutboxRow.dedupe_key])
            .returning(NotificationOutboxRow)
        )
        try:
            async with self._session.begin_nested():
                row = (await self._session.scalars(statement)).one_or_none()
                if row is not None:
                    await self._session.execute(
                        select(func.pg_notify(NOTIFICATION_WAKEUP_CHANNEL, "due"))
                    )
        except IntegrityError as exc:
            raise ConflictError("notification identifier already exists") from exc
        return None if row is None else notification_to_domain(row)

    async def claim_due(
        self,
        now: datetime,
        limit: int,
        claimant: str,
        lease_seconds: float,
        providers: frozenset[PushProvider],
    ) -> list[Notification]:
        instant = _aware_utc(now)
        if not claimant.strip():
            raise ValueError("notification claimant cannot be blank")
        if lease_seconds <= 0:
            raise ValueError("notification lease must be positive")
        if not providers:
            raise ValueError("notification claim requires at least one provider")
        provider_values = {provider.value for provider in providers}
        locked = (
            await self._session.scalars(
                select(NotificationOutboxRow)
                .where(
                    NotificationOutboxRow.status == NotificationStatus.PENDING.value,
                    NotificationOutboxRow.next_attempt_at <= instant,
                    or_(
                        NotificationOutboxRow.claimed_until.is_(None),
                        NotificationOutboxRow.claimed_until <= instant,
                    ),
                    or_(
                        ~_pending_target_exists(),
                        _pending_target_exists(provider_values),
                    ),
                )
                .order_by(
                    NotificationOutboxRow.next_attempt_at,
                    NotificationOutboxRow.created_at,
                    NotificationOutboxRow.id,
                )
                .limit(_positive_limit(limit, "notification claim"))
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not locked:
            return []
        identities = [row.id for row in locked]
        rows = (
            await self._session.scalars(
                update(NotificationOutboxRow)
                .where(NotificationOutboxRow.id.in_(identities))
                .values(
                    attempts=NotificationOutboxRow.attempts + 1,
                    claimed_by=claimant,
                    claimed_until=instant + timedelta(seconds=lease_seconds),
                )
                .returning(NotificationOutboxRow)
            )
        ).all()
        values = [notification_to_domain(row) for row in rows]
        values.sort(key=lambda value: (value.next_attempt_at, value.created_at, value.id))
        return values

    async def list_pending_older_than(
        self,
        before: datetime,
        limit: int,
    ) -> list[Notification]:
        rows = (
            await self._session.scalars(
                select(NotificationOutboxRow)
                .where(
                    NotificationOutboxRow.status == NotificationStatus.PENDING.value,
                    NotificationOutboxRow.created_at < _aware_utc(before),
                )
                .order_by(NotificationOutboxRow.created_at, NotificationOutboxRow.id)
                .limit(_positive_limit(limit, "notification backlog"))
            )
        ).all()
        return [notification_to_domain(row) for row in rows]

    async def record_delivery(self, delivery: NotificationDelivery) -> None:
        owner = await self._session.scalar(
            select(NotificationOutboxRow.id)
            .join(
                DeviceRow,
                and_(
                    DeviceRow.id == delivery.device_id,
                    DeviceRow.tenant_id == NotificationOutboxRow.tenant_id,
                    DeviceRow.principal_id == NotificationOutboxRow.principal_id,
                ),
            )
            .where(NotificationOutboxRow.id == delivery.notification_id)
        )
        if owner is None:
            raise NotFoundError("notification delivery owner not found")
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    pg_insert(NotificationDeliveryRow).values(
                        **notification_delivery_values(delivery)
                    )
                )
        except IntegrityError as exc:
            raise ConflictError("notification delivery already exists") from exc

    async def list_deliveries(self, notification_id: UUID) -> list[NotificationDelivery]:
        exists = await self._session.scalar(
            select(NotificationOutboxRow.id).where(NotificationOutboxRow.id == notification_id)
        )
        if exists is None:
            raise NotFoundError("notification not found")
        rows = (
            await self._session.scalars(
                select(NotificationDeliveryRow)
                .where(NotificationDeliveryRow.notification_id == notification_id)
                .order_by(
                    NotificationDeliveryRow.attempt,
                    NotificationDeliveryRow.device_id,
                )
            )
        ).all()
        return [notification_delivery_to_domain(row) for row in rows]

    async def list_deliveries_for(
        self,
        notification_ids: tuple[UUID, ...],
    ) -> dict[UUID, list[NotificationDelivery]]:
        if not notification_ids:
            return {}
        existing = set(
            await self._session.scalars(
                select(NotificationOutboxRow.id).where(
                    NotificationOutboxRow.id.in_(notification_ids)
                )
            )
        )
        if existing != set(notification_ids):
            raise NotFoundError("notification not found")
        rows = (
            await self._session.scalars(
                select(NotificationDeliveryRow)
                .where(NotificationDeliveryRow.notification_id.in_(notification_ids))
                .order_by(
                    NotificationDeliveryRow.notification_id,
                    NotificationDeliveryRow.attempt,
                    NotificationDeliveryRow.device_id,
                )
            )
        ).all()
        grouped: dict[UUID, list[NotificationDelivery]] = {
            notification_id: [] for notification_id in notification_ids
        }
        for row in rows:
            grouped[row.notification_id].append(notification_delivery_to_domain(row))
        return grouped

    async def settle(
        self,
        notification_id: UUID,
        attempt: int,
        status: NotificationStatus,
        next_attempt_at: datetime | None,
    ) -> bool:
        if attempt <= 0:
            raise ValueError("notification settlement attempt must be positive")
        values: dict[str, object]
        if status is NotificationStatus.PENDING:
            if next_attempt_at is None:
                raise ValueError("pending notification requires next attempt")
            values = {
                "next_attempt_at": _aware_utc(next_attempt_at),
                "claimed_by": None,
                "claimed_until": None,
            }
        else:
            if next_attempt_at is not None:
                raise ValueError("settled notification cannot have a next attempt")
            values = {
                "status": status.value,
                "claimed_by": None,
                "claimed_until": None,
                "settled_at": _aware_utc(self._clock.now()),
            }
        identity = await self._session.scalar(
            update(NotificationOutboxRow)
            .where(
                NotificationOutboxRow.id == notification_id,
                NotificationOutboxRow.status == NotificationStatus.PENDING.value,
                NotificationOutboxRow.attempts == attempt,
                NotificationOutboxRow.claimed_by.is_not(None),
            )
            .values(**values)
            .returning(NotificationOutboxRow.id)
        )
        if identity is None:
            exists = await self._session.scalar(
                select(NotificationOutboxRow.id).where(NotificationOutboxRow.id == notification_id)
            )
            if exists is None:
                raise NotFoundError("notification not found")
            return False
        return True

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: NotificationCursor | None = None,
    ) -> list[Notification]:
        statement = select(NotificationOutboxRow).where(
            NotificationOutboxRow.tenant_id == principal.tenant_id,
            NotificationOutboxRow.principal_id == principal.principal_id,
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    NotificationOutboxRow.created_at < cursor.created_at,
                    and_(
                        NotificationOutboxRow.created_at == cursor.created_at,
                        NotificationOutboxRow.id < cursor.id,
                    ),
                )
            )
        rows = (
            await self._session.scalars(
                statement.order_by(
                    NotificationOutboxRow.created_at.desc(),
                    NotificationOutboxRow.id.desc(),
                ).limit(_positive_limit(limit, "notification"))
            )
        ).all()
        return [notification_to_domain(row) for row in rows]


def _pending_target_exists(provider_values: set[str] | None = None) -> ColumnElement[bool]:
    device = aliased(DeviceRow)
    delivery = aliased(NotificationDeliveryRow)
    terminal_delivery = exists(
        select(delivery.id).where(
            delivery.notification_id == NotificationOutboxRow.id,
            delivery.device_id == device.id,
            delivery.outcome != DeliveryOutcome.RETRY.value,
        )
    ).correlate(NotificationOutboxRow, device)
    conditions: list[ColumnElement[bool]] = [
        device.tenant_id == NotificationOutboxRow.tenant_id,
        device.principal_id == NotificationOutboxRow.principal_id,
        device.status == DeviceStatus.ACTIVE.value,
        device.push_provider.is_not(None),
        device.push_token.is_not(None),
        ~device.muted_kinds.op("@>")(func.jsonb_build_array(NotificationOutboxRow.kind)),
        ~terminal_delivery,
        or_(
            NotificationOutboxRow.kind != NotificationKind.TEST.value,
            NotificationOutboxRow.dedupe_key.like(
                func.concat(
                    TEST_NOTIFICATION_DEDUPE_PREFIX,
                    cast(device.id, String),
                    ":%",
                )
            ),
        ),
    ]
    if provider_values is not None:
        conditions.append(device.push_provider.in_(provider_values))
    return exists(select(device.id).where(*conditions)).correlate(NotificationOutboxRow)


def _owned_by(device: Device, principal: Principal) -> bool:
    return device.tenant_id == principal.tenant_id and device.principal_id == principal.principal_id


def _positive_limit(limit: int, subject: str) -> int:
    if limit <= 0:
        raise ValueError(f"{subject} limit must be positive")
    return limit


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification persistence requires an aware instant")
    return value.astimezone(UTC)


def _pending_notification(value: NewNotification) -> Notification:
    return Notification(
        **value.model_dump(),
        status=NotificationStatus.PENDING,
        attempts=0,
    )
