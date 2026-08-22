"""Principal-scoped device lifecycle and durable notification inbox."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.devices import (
    Device,
    DeviceCursor,
    DeviceRegistration,
    DeviceRegistrationIdempotencyRecord,
    DeviceStatus,
    PushEnvironment,
    PushProvider,
    push_token_fingerprint,
)
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.notifications import (
    NOTIFICATION_TITLES,
    NewNotification,
    NotificationCursor,
    NotificationKind,
    NotificationPayload,
    device_test_key,
)
from agent_core.domain.views import (
    DeviceRegistrationResult,
    DeviceView,
    NotificationInboxItem,
    Page,
    TestNotificationResult,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class DeviceManagementService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        notification_expiry_seconds: float,
    ) -> None:
        if notification_expiry_seconds <= 0:
            raise ValueError("test notification expiry must be positive")
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._notification_expiry = notification_expiry_seconds

    async def register(
        self,
        principal: Principal,
        registration: DeviceRegistration,
        idempotency_key: str | None = None,
    ) -> DeviceRegistrationResult:
        require_scope(principal, "device.write")
        key = _optional_idempotency_key(idempotency_key)
        request_hash = None if key is None else _registration_hash(registration)
        now = self._clock.now()
        try:
            async with self._uow_factory() as uow:
                if key is not None:
                    assert request_hash is not None
                    replay = await uow.device_registration_idempotency.get(
                        principal.tenant_id,
                        principal.principal_id,
                        key,
                    )
                    if replay is not None:
                        return _registration_replay(replay, request_hash)
                existing = await uow.devices.get_by_client_device_id(
                    registration.client_device_id,
                    principal,
                )
                new_routing = _routing(registration)
                old_routing = None if existing is None else _device_routing(existing)
                routing_changed = existing is not None and old_routing != new_routing
                device = Device(
                    id=self._ids.new_id() if existing is None else existing.id,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    client_device_id=registration.client_device_id,
                    name=registration.name,
                    kind=registration.kind,
                    platform=registration.platform,
                    app_bundle_id=registration.app_bundle_id,
                    push_provider=registration.push_provider,
                    push_token=registration.push_token,
                    push_environment=registration.push_environment,
                    push_token_updated_at=(
                        now
                        if registration.push_token is not None
                        and (existing is None or routing_changed)
                        else None
                        if existing is None
                        else existing.push_token_updated_at
                    ),
                    push_token_invalidated_at=(
                        None
                        if routing_changed
                        else None
                        if existing is None
                        else existing.push_token_invalidated_at
                    ),
                    muted_kinds=registration.muted_kinds,
                    status=DeviceStatus.ACTIVE,
                    revoked_at=None,
                    last_seen_at=now,
                    created_at=now if existing is None else existing.created_at,
                    updated_at=now,
                )
                stored = await uow.devices.upsert(device, principal)
                fingerprint = _fingerprint(stored)
                if existing is None:
                    await self._append_lifecycle(
                        uow,
                        principal,
                        stored,
                        event_type="device.registered",
                        derivation_key=f"device.registered:{stored.id}",
                        token_fingerprint=fingerprint,
                    )
                elif routing_changed:
                    await self._append_lifecycle(
                        uow,
                        principal,
                        stored,
                        event_type="device.push_token_updated",
                        derivation_key=(f"device.push_token_updated:{stored.id}:{now.isoformat()}"),
                        token_fingerprint=fingerprint,
                    )
                view = _device_view(stored)
                if key is not None:
                    assert request_hash is not None
                    await uow.device_registration_idempotency.create(
                        DeviceRegistrationIdempotencyRecord(
                            tenant_id=principal.tenant_id,
                            principal_id=principal.principal_id,
                            key=key,
                            request_hash=request_hash,
                            response=view.model_dump(mode="json"),
                            created_at=now,
                        )
                    )
            return DeviceRegistrationResult(device=view, replayed=existing is not None)
        except ConflictError:
            if key is None or request_hash is None:
                raise
            async with self._uow_factory() as uow:
                replay = await uow.device_registration_idempotency.get(
                    principal.tenant_id,
                    principal.principal_id,
                    key,
                )
            if replay is None:
                raise
            return _registration_replay(replay, request_hash)

    async def get(self, principal: Principal, device_id: UUID) -> DeviceView:
        require_scope(principal, "device.read")
        async with self._uow_factory() as uow:
            return _device_view(await uow.devices.get(device_id, principal))

    async def list(
        self,
        principal: Principal,
        limit: int,
        cursor: str | None,
    ) -> Page[DeviceView]:
        require_scope(principal, "device.read")
        if limit <= 0:
            raise ValueError("device list limit must be positive")
        parsed = _decode_device_cursor(cursor)
        async with self._uow_factory() as uow:
            rows = await uow.devices.list(principal, limit=limit + 1, cursor=parsed)
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            tail = visible[-1]
            next_cursor = _encode_cursor(
                {"created_at": tail.created_at.isoformat(), "id": str(tail.id)}
            )
        return Page(items=[_device_view(row) for row in visible], next_cursor=next_cursor)

    async def revoke(self, principal: Principal, device_id: UUID) -> DeviceView:
        require_scope(principal, "device.write")
        now = self._clock.now()
        async with self._uow_factory() as uow:
            current = await uow.devices.get(device_id, principal)
            if current.status is DeviceStatus.REVOKED:
                return _device_view(current)
            fingerprint = _fingerprint(current)
            revoked = await uow.devices.revoke(device_id, principal, now)
            await self._append_lifecycle(
                uow,
                principal,
                revoked,
                event_type="device.revoked",
                derivation_key=f"device.revoked:{device_id}",
                token_fingerprint=fingerprint,
            )
        return _device_view(revoked)

    async def delete(self, principal: Principal, device_id: UUID) -> None:
        require_scope(principal, "device.write")
        async with self._uow_factory() as uow:
            current = await uow.devices.get(device_id, principal)
            fingerprint = _fingerprint(current)
            await uow.devices.delete(device_id, principal)
            await self._append_lifecycle(
                uow,
                principal,
                current,
                event_type="device.deleted",
                derivation_key=f"device.deleted:{device_id}",
                token_fingerprint=fingerprint,
            )

    async def enqueue_test_notification(
        self,
        principal: Principal,
        device_id: UUID,
        idempotency_key: str,
    ) -> TestNotificationResult:
        require_scope(principal, "device.write")
        dedupe_key = device_test_key(device_id, idempotency_key)
        now = self._clock.now()
        async with self._uow_factory() as uow:
            device = await uow.devices.get(device_id, principal)
            if device.status is not DeviceStatus.ACTIVE or device.push_token is None:
                raise ConflictError(
                    "device has no active push target",
                    reason="device.push_target_inactive",
                )
            notification_id = self._ids.new_id()
            stored = await uow.notification_outbox.enqueue(
                NewNotification(
                    id=notification_id,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    kind=NotificationKind.TEST,
                    dedupe_key=dedupe_key,
                    payload=NotificationPayload(
                        kind=NotificationKind.TEST,
                        title=NOTIFICATION_TITLES[NotificationKind.TEST],
                        notification_id=notification_id,
                    ),
                    priority=10,
                    expires_at=now + timedelta(seconds=self._notification_expiry),
                    next_attempt_at=now,
                    created_at=now,
                )
            )
        return TestNotificationResult(
            notification_id=None if stored is None else stored.id,
            replayed=stored is None,
        )

    async def _append_lifecycle(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        device: Device,
        *,
        event_type: str,
        derivation_key: str,
        token_fingerprint: str | None,
    ) -> None:
        await uow.process_events.append(
            ProcessEvent(
                id=self._ids.new_id(),
                event_type=event_type,
                actor_type="api",
                actor_id=principal.principal_id,
                payload={
                    "tenant_id": principal.tenant_id,
                    "principal_id": principal.principal_id,
                    "device_id": str(device.id),
                    "client_device_id": device.client_device_id,
                    "token_fingerprint": token_fingerprint,
                },
                derivation_key=derivation_key,
                created_at=self._clock.now(),
            )
        )


class NotificationInboxService:
    def __init__(self, *, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def list(
        self,
        principal: Principal,
        limit: int,
        cursor: str | None,
    ) -> Page[NotificationInboxItem]:
        require_scope(principal, "notification.read")
        if limit <= 0:
            raise ValueError("notification list limit must be positive")
        parsed = _decode_notification_cursor(cursor)
        async with self._uow_factory() as uow:
            rows = await uow.notification_outbox.list(
                principal,
                limit=limit + 1,
                cursor=parsed,
            )
            visible = rows[:limit]
            deliveries = await uow.notification_outbox.list_deliveries_for(
                tuple(row.id for row in visible)
            )
            items = [
                NotificationInboxItem(
                    notification=row,
                    deliveries=deliveries[row.id],
                )
                for row in visible
            ]
        next_cursor = None
        if len(rows) > limit and visible:
            tail = visible[-1]
            next_cursor = _encode_cursor(
                {"created_at": tail.created_at.isoformat(), "id": str(tail.id)}
            )
        return Page(items=items, next_cursor=next_cursor)


def _routing(
    registration: DeviceRegistration,
) -> tuple[PushProvider | None, str | None, PushEnvironment | None]:
    return (
        registration.push_provider,
        None if registration.push_token is None else registration.push_token.get_secret_value(),
        registration.push_environment,
    )


def _device_routing(
    device: Device,
) -> tuple[PushProvider | None, str | None, PushEnvironment | None]:
    return (
        device.push_provider,
        None if device.push_token is None else device.push_token.get_secret_value(),
        device.push_environment,
    )


def _fingerprint(device: Device) -> str | None:
    if device.push_token is None:
        return None
    return push_token_fingerprint(device.push_token.get_secret_value())


def _device_view(device: Device) -> DeviceView:
    return DeviceView(
        id=device.id,
        client_device_id=device.client_device_id,
        name=device.name,
        kind=device.kind,
        platform=device.platform,
        app_bundle_id=device.app_bundle_id,
        push_provider=device.push_provider,
        push_environment=device.push_environment,
        push_token_fingerprint=_fingerprint(device),
        push_token_updated_at=device.push_token_updated_at,
        push_token_invalidated_at=device.push_token_invalidated_at,
        muted_kinds=device.muted_kinds,
        status=device.status,
        revoked_at=device.revoked_at,
        last_seen_at=device.last_seen_at,
        created_at=device.created_at,
        updated_at=device.updated_at,
    )


def _optional_idempotency_key(value: str | None) -> str | None:
    if value is not None and (not value.strip() or len(value) > 255):
        raise ValueError("device idempotency key must contain 1 to 255 characters")
    return None if value is None else value.strip()


def _registration_hash(registration: DeviceRegistration) -> str:
    payload = registration.model_dump(mode="json")
    payload["muted_kinds"] = sorted(kind.value for kind in registration.muted_kinds)
    payload["push_token"] = (
        None if registration.push_token is None else registration.push_token.get_secret_value()
    )
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _registration_replay(
    record: DeviceRegistrationIdempotencyRecord,
    request_hash: str,
) -> DeviceRegistrationResult:
    if record.request_hash != request_hash:
        raise ConflictError(
            "device idempotency key was reused with different content",
            reason="device.idempotency_mismatch",
        )
    return DeviceRegistrationResult(
        device=DeviceView.model_validate(record.response),
        replayed=True,
    )


def _encode_cursor(payload: dict[str, str]) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def _decode_device_cursor(value: str | None) -> DeviceCursor | None:
    if value is None:
        return None
    payload = _decode_cursor(value)
    return DeviceCursor.model_validate(payload)


def _decode_notification_cursor(value: str | None) -> NotificationCursor | None:
    if value is None:
        return None
    payload = _decode_cursor(value)
    return NotificationCursor.model_validate(payload)


def _decode_cursor(value: str) -> object:
    try:
        padding = "=" * (-len(value) % 4)
        return json.loads(base64.urlsafe_b64decode(value + padding))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is malformed") from exc
