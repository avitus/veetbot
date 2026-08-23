"""Durable notification dispatch orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalStatus
from agent_core.domain.devices import PushProvider, PushTarget, push_token_fingerprint
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import ProcessEvent
from agent_core.domain.notifications import (
    DeliveryOutcome,
    Notification,
    NotificationDelivery,
    NotificationKind,
    NotificationStatus,
    PushMessage,
    PushOutcome,
)
from agent_core.domain.runs import RunStatus
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.devices import DeviceRegistry
from agent_core.ports.events import ProcessEventRepository
from agent_core.ports.notifications import NotificationOutbox, PushTransport
from agent_core.ports.repositories import (
    ApprovalRepository,
    CheckpointRepository,
    RunRepository,
    SessionRepository,
)

DispatchProbe = Callable[[str], None]
logger = logging.getLogger(__name__)
_PENDING_BACKLOG_ALERT_AFTER = timedelta(minutes=5)


class DispatchProbeError(RuntimeError):
    """Injected crash boundary that must escape the worker scan."""


class NotificationDispatchUnitOfWork(Protocol):
    approvals: ApprovalRepository
    checkpoints: CheckpointRepository
    devices: DeviceRegistry
    notification_outbox: NotificationOutbox
    process_events: ProcessEventRepository
    runs: RunRepository
    sessions: SessionRepository


type NotificationDispatchUnitOfWorkFactory = Callable[
    [], AbstractAsyncContextManager[NotificationDispatchUnitOfWork]
]


class NotificationDispatcher:
    """Claim and deliver one bounded batch without holding I/O transactions."""

    def __init__(
        self,
        *,
        uow_factory: NotificationDispatchUnitOfWorkFactory,
        transport: PushTransport,
        providers: frozenset[PushProvider],
        clock: Clock,
        ids: IdFactory,
        claimant: str,
        batch_size: int = 100,
        lease_seconds: float = 30,
        retry_delays: tuple[float, ...] = (30, 120, 600, 3600),
        dispatch_probe: DispatchProbe | None = None,
    ) -> None:
        if not providers:
            raise ValueError("notification dispatcher requires at least one provider")
        if batch_size <= 0 or lease_seconds <= 0:
            raise ValueError("notification dispatch limits must be positive")
        if not claimant.strip():
            raise ValueError("notification claimant cannot be blank")
        if not retry_delays or any(delay <= 0 for delay in retry_delays):
            raise ValueError("notification retry delays must be positive")
        self._uow_factory = uow_factory
        self._transport = transport
        self._providers = providers
        self._clock = clock
        self._ids = ids
        self._claimant = claimant
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._retry_delays = retry_delays
        self._dispatch_probe = dispatch_probe or (lambda _boundary: None)

    async def run_once(self) -> int:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            backlog = await uow.notification_outbox.list_pending_older_than(
                now - _PENDING_BACKLOG_ALERT_AFTER,
                self._batch_size,
            )
            claimed = await uow.notification_outbox.claim_due(
                now,
                self._batch_size,
                self._claimant,
                self._lease_seconds,
                self._providers,
            )
        for notification in backlog:
            logger.warning(
                "notification pending backlog exceeded threshold",
                extra={
                    "notification_id": str(notification.id),
                    "notification_kind": notification.kind.value,
                    "notification_created_at": notification.created_at.isoformat(),
                    "claimant": self._claimant,
                },
            )
        for notification in claimed:
            try:
                await self._dispatch(notification)
            except DispatchProbeError:
                raise
            except Exception:
                logger.exception(
                    "notification dispatch failed notification_id=%s",
                    notification.id,
                )
        return len(claimed)

    async def _dispatch(self, notification: Notification) -> None:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            if await self._is_stale(uow, notification, now):
                await uow.notification_outbox.settle(
                    notification.id,
                    NotificationStatus.SUPERSEDED,
                    None,
                )
                return
            if notification.expires_at is not None and notification.expires_at <= now:
                await uow.notification_outbox.settle(
                    notification.id,
                    NotificationStatus.EXPIRED,
                    None,
                )
                return
            targets = await uow.devices.push_targets(
                notification.tenant_id,
                notification.principal_id,
                notification.kind,
            )
            if notification.kind is NotificationKind.TEST:
                target_device_id = _test_target_device_id(notification.dedupe_key)
                targets = [target for target in targets if target.device_id == target_device_id]
            deliveries = await uow.notification_outbox.list_deliveries(notification.id)

        terminal_devices = {
            delivery.device_id
            for delivery in deliveries
            if delivery.outcome is not DeliveryOutcome.RETRY
        }
        pending_targets = [
            target
            for target in targets
            if target.device_id not in terminal_devices and target.provider in self._providers
        ]
        other_targets = [
            target
            for target in targets
            if target.device_id not in terminal_devices and target.provider not in self._providers
        ]
        if not pending_targets:
            async with self._uow_factory() as uow:
                await uow.notification_outbox.settle(
                    notification.id,
                    NotificationStatus.PENDING if other_targets else NotificationStatus.DISPATCHED,
                    now if other_targets else None,
                )
            return

        outcomes: list[tuple[PushTarget, PushOutcome]] = []
        message = PushMessage(
            notification_id=notification.id,
            dedupe_key=notification.dedupe_key,
            payload=notification.payload,
            priority=notification.priority,
            expires_at=notification.expires_at,
        )
        for target in pending_targets:
            try:
                outcome = await self._transport.deliver(target, message)
            except DispatchProbeError:
                raise
            except Exception:
                outcome = PushOutcome(
                    outcome=DeliveryOutcome.RETRY,
                    provider_reason="TransportError",
                )
                outcomes.append((target, outcome))
                continue
            outcomes.append((target, outcome))
            try:
                self._dispatch_probe("transport_accepted")
            except Exception as exc:
                raise DispatchProbeError(str(exc)) from exc

        async with self._uow_factory() as uow:
            saw_retry = False
            saw_rejection = False
            for target, outcome in outcomes:
                await uow.notification_outbox.record_delivery(
                    NotificationDelivery(
                        id=self._ids.new_id(),
                        notification_id=notification.id,
                        device_id=target.device_id,
                        attempt=notification.attempts,
                        outcome=outcome.outcome,
                        provider_reason=outcome.provider_reason,
                        provider_id=outcome.provider_id,
                        attempted_at=now,
                    )
                )
                if outcome.outcome is DeliveryOutcome.RETRY:
                    saw_retry = True
                elif outcome.outcome is DeliveryOutcome.REJECTED:
                    saw_rejection = True
                elif outcome.outcome is DeliveryOutcome.UNREGISTERED:
                    await self._invalidate_token(uow, target, outcome, now)

            if saw_retry:
                retry_index = notification.attempts - 1
                if retry_index < len(self._retry_delays):
                    await uow.notification_outbox.settle(
                        notification.id,
                        NotificationStatus.PENDING,
                        now + timedelta(seconds=self._retry_delays[retry_index]),
                    )
                else:
                    await uow.notification_outbox.settle(
                        notification.id,
                        NotificationStatus.FAILED,
                        None,
                    )
            elif saw_rejection:
                await uow.notification_outbox.settle(
                    notification.id,
                    NotificationStatus.FAILED,
                    None,
                )
            elif other_targets:
                await uow.notification_outbox.settle(
                    notification.id,
                    NotificationStatus.PENDING,
                    now,
                )
            else:
                await uow.notification_outbox.settle(
                    notification.id,
                    NotificationStatus.DISPATCHED,
                    None,
                )

    async def _is_stale(
        self,
        uow: NotificationDispatchUnitOfWork,
        notification: Notification,
        now: datetime,
    ) -> bool:
        principal = Principal(
            tenant_id=notification.tenant_id,
            principal_id=notification.principal_id,
        )
        try:
            if notification.session_id is not None:
                await uow.sessions.get(notification.session_id, principal)
            if notification.kind is NotificationKind.APPROVAL_REQUESTED:
                assert notification.approval_id is not None
                approval = await uow.approvals.get(notification.approval_id, principal)
                return approval.status is not ApprovalStatus.PENDING or (
                    approval.expires_at is not None and approval.expires_at <= now
                )
            if notification.run_id is None:
                return False
            run = await uow.runs.get(notification.run_id, principal)
            if notification.kind is NotificationKind.QUESTION_ASKED:
                if run.status is not RunStatus.WAITING_FOR_USER:
                    return True
                checkpoint = await uow.checkpoints.latest(run.id)
                if checkpoint is None:
                    return True
                return checkpoint.working_state.get("outstanding_question_id") != str(
                    notification.question_id
                )
            if notification.kind is NotificationKind.RUN_FAILED:
                return run.status is not RunStatus.FAILED
            if notification.kind is NotificationKind.SCHEDULE_RUN_FINISHED:
                return run.status not in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }
        except NotFoundError:
            return True
        return False

    async def _invalidate_token(
        self,
        uow: NotificationDispatchUnitOfWork,
        target: PushTarget,
        outcome: PushOutcome,
        now: datetime,
    ) -> None:
        fingerprint = push_token_fingerprint(target.token.get_secret_value())
        invalidated = await uow.devices.invalidate_push_token(
            target.device_id,
            outcome.provider_reason or "Unregistered",
            now,
        )
        if invalidated is None:
            return
        await uow.process_events.append(
            ProcessEvent(
                id=self._ids.new_id(),
                event_type="device.push_token_invalidated",
                actor_type="notify",
                actor_id=invalidated.principal_id,
                payload={
                    "tenant_id": invalidated.tenant_id,
                    "principal_id": invalidated.principal_id,
                    "device_id": str(invalidated.id),
                    "token_fingerprint": fingerprint,
                    "reason_code": "device.push_token_unregistered",
                },
                derivation_key=(f"device.push_token_invalidated:{invalidated.id}:{fingerprint}"),
                created_at=now,
            )
        )


def _test_target_device_id(dedupe_key: str) -> UUID | None:
    prefix = "device.test:"
    if not dedupe_key.startswith(prefix):
        return None
    raw_device_id, separator, _idempotency_key = dedupe_key.removeprefix(prefix).partition(":")
    if not separator:
        return None
    try:
        return UUID(raw_device_id)
    except ValueError:
        return None
