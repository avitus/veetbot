"""Transactional notification production for existing run and schedule transitions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from agent_core.domain.devices import Device, DeviceInvocation, DeviceInvocationStatus
from agent_core.domain.events import ProcessEvent
from agent_core.domain.notifications import (
    NOTIFICATION_TITLES,
    DeviceInvocationSubjectStatus,
    NewNotification,
    NotificationKind,
    NotificationPayload,
    approval_requested_key,
    device_invocation_key,
    question_asked_key,
    run_failed_key,
    schedule_occurrence_skipped_key,
    schedule_run_finished_key,
)
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.schedules import OccurrenceDisposition, Schedule, ScheduleOccurrence
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.events import ProcessEventRepository
from agent_core.ports.notifications import NotificationOutbox


class NotificationProductionUnitOfWork(Protocol):
    """The repositories required inside a producer's existing transaction."""

    process_events: ProcessEventRepository
    notification_outbox: NotificationOutbox


class NotificationProducer:
    """Create content-free outbox rows for the closed transition catalog."""

    def __init__(self, *, clock: Clock, ids: IdFactory) -> None:
        self._clock = clock
        self._ids = ids

    async def for_run_transition(
        self,
        uow: NotificationProductionUnitOfWork,
        *,
        run: Run,
        principal_id: str,
        status: RunStatus,
        approval_id: UUID | None = None,
        question_id: UUID | None = None,
        approval_expires_at: datetime | None = None,
    ) -> bool:
        now = self._clock.now()
        notification_id = self._ids.new_id()
        if status is RunStatus.WAITING_FOR_APPROVAL:
            if approval_id is None:
                raise ValueError("approval notification requires an approval identifier")
            kind = NotificationKind.APPROVAL_REQUESTED
            dedupe_key = approval_requested_key(approval_id)
            priority = 10
        elif status is RunStatus.WAITING_FOR_USER:
            if question_id is None:
                raise ValueError("question notification requires a question identifier")
            kind = NotificationKind.QUESTION_ASKED
            dedupe_key = question_asked_key(run.id, question_id)
            priority = 10
        elif status is RunStatus.FAILED:
            kind = NotificationKind.RUN_FAILED
            dedupe_key = run_failed_key(run.id)
            priority = 5
        else:
            return False
        return await self._enqueue_or_audit(
            uow,
            lambda: NewNotification(
                id=notification_id,
                tenant_id=run.tenant_id,
                principal_id=principal_id,
                kind=kind,
                dedupe_key=dedupe_key,
                session_id=run.session_id,
                run_id=run.id,
                approval_id=approval_id,
                question_id=question_id,
                payload=NotificationPayload(
                    kind=kind,
                    title=NOTIFICATION_TITLES[kind],
                    status=status,
                    session_id=run.session_id,
                    run_id=run.id,
                    approval_id=approval_id,
                    question_id=question_id,
                    notification_id=notification_id,
                ),
                priority=priority,
                expires_at=(
                    approval_expires_at
                    if kind is NotificationKind.APPROVAL_REQUESTED
                    else now + timedelta(hours=24)
                ),
                next_attempt_at=now,
                created_at=now,
            ),
            failure_context={
                "tenant_id": run.tenant_id,
                "principal_id": principal_id,
                "notification_id": notification_id,
                "session_id": run.session_id,
                "run_id": run.id,
                "approval_id": approval_id,
                "question_id": question_id,
            },
            dedupe_key=dedupe_key,
        )

    async def for_schedule_run_accounted(
        self,
        uow: NotificationProductionUnitOfWork,
        *,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
        run: Run,
    ) -> bool:
        if (
            occurrence.disposition is not OccurrenceDisposition.MATERIALIZED
            or occurrence.session_id is None
            or occurrence.run_id is None
            or occurrence.run_id != run.id
            or occurrence.session_id != run.session_id
            or run.status
            not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }
        ):
            raise ValueError("schedule accounting notification requires one terminal linked run")
        now = self._clock.now()
        notification_id = self._ids.new_id()
        kind = NotificationKind.SCHEDULE_RUN_FINISHED
        return await self._enqueue_or_audit(
            uow,
            lambda: NewNotification(
                id=notification_id,
                tenant_id=schedule.tenant_id,
                principal_id=schedule.principal_id,
                kind=kind,
                dedupe_key=schedule_run_finished_key(occurrence.id),
                session_id=run.session_id,
                run_id=run.id,
                schedule_id=schedule.id,
                occurrence_id=occurrence.id,
                payload=NotificationPayload(
                    kind=kind,
                    title=NOTIFICATION_TITLES[kind],
                    status=run.status,
                    session_id=run.session_id,
                    run_id=run.id,
                    schedule_id=schedule.id,
                    occurrence_id=occurrence.id,
                    notification_id=notification_id,
                ),
                priority=5,
                expires_at=now + timedelta(hours=24),
                next_attempt_at=now,
                created_at=now,
            ),
            failure_context={
                "tenant_id": schedule.tenant_id,
                "principal_id": schedule.principal_id,
                "notification_id": notification_id,
                "session_id": run.session_id,
                "run_id": run.id,
                "schedule_id": schedule.id,
                "occurrence_id": occurrence.id,
            },
            dedupe_key=schedule_run_finished_key(occurrence.id),
        )

    async def for_schedule_occurrence(
        self,
        uow: NotificationProductionUnitOfWork,
        *,
        schedule: Schedule,
        occurrence: ScheduleOccurrence,
    ) -> bool:
        if occurrence.disposition is OccurrenceDisposition.MATERIALIZED:
            return False
        if occurrence.disposition not in {
            OccurrenceDisposition.MISSED,
            OccurrenceDisposition.SKIPPED_OVERLAP,
            OccurrenceDisposition.AUTHORIZATION_FAILED,
            OccurrenceDisposition.CONFIGURATION_FAILED,
        }:
            raise ValueError("unsupported schedule occurrence disposition")
        now = self._clock.now()
        notification_id = self._ids.new_id()
        kind = NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED
        return await self._enqueue_or_audit(
            uow,
            lambda: NewNotification(
                id=notification_id,
                tenant_id=schedule.tenant_id,
                principal_id=schedule.principal_id,
                kind=kind,
                dedupe_key=schedule_occurrence_skipped_key(occurrence.id),
                schedule_id=schedule.id,
                occurrence_id=occurrence.id,
                payload=NotificationPayload(
                    kind=kind,
                    title=NOTIFICATION_TITLES[kind],
                    status=occurrence.disposition,
                    schedule_id=schedule.id,
                    occurrence_id=occurrence.id,
                    notification_id=notification_id,
                ),
                priority=5,
                expires_at=now + timedelta(hours=24),
                next_attempt_at=now,
                created_at=now,
            ),
            failure_context={
                "tenant_id": schedule.tenant_id,
                "principal_id": schedule.principal_id,
                "notification_id": notification_id,
                "schedule_id": schedule.id,
                "occurrence_id": occurrence.id,
            },
            dedupe_key=schedule_occurrence_skipped_key(occurrence.id),
        )

    async def for_device_invocation(
        self,
        uow: NotificationProductionUnitOfWork,
        *,
        invocation: DeviceInvocation,
        device: Device,
    ) -> bool:
        if invocation.device_id != device.id or invocation.tenant_id != device.tenant_id:
            raise ValueError("device invocation notification requires the invocation's own device")
        if invocation.status is not DeviceInvocationStatus.PENDING:
            raise ValueError("device invocation notification requires a pending invocation")
        now = self._clock.now()
        notification_id = self._ids.new_id()
        kind = NotificationKind.DEVICE_INVOCATION
        dedupe_key = device_invocation_key(invocation.id)
        return await self._enqueue_or_audit(
            uow,
            lambda: NewNotification(
                id=notification_id,
                tenant_id=device.tenant_id,
                principal_id=device.principal_id,
                kind=kind,
                dedupe_key=dedupe_key,
                payload=NotificationPayload(
                    kind=kind,
                    title=NOTIFICATION_TITLES[kind],
                    status=DeviceInvocationSubjectStatus.PENDING,
                    invocation_id=invocation.id,
                    device_id=invocation.device_id,
                    notification_id=notification_id,
                ),
                priority=10,
                expires_at=now + timedelta(hours=24),
                next_attempt_at=now,
                created_at=now,
            ),
            failure_context={
                "tenant_id": device.tenant_id,
                "principal_id": device.principal_id,
                "notification_id": notification_id,
                "invocation_id": invocation.id,
                "device_id": invocation.device_id,
            },
            dedupe_key=dedupe_key,
        )

    async def _enqueue_or_audit(
        self,
        uow: NotificationProductionUnitOfWork,
        notification_factory: Callable[[], NewNotification],
        *,
        failure_context: dict[str, str | UUID | None],
        dedupe_key: str,
    ) -> bool:
        try:
            notification = notification_factory()
            stored = await uow.notification_outbox.enqueue(notification)
        except Exception:
            now = self._clock.now()
            principal_id = str(failure_context["principal_id"])
            payload = {
                key: None if value is None else str(value) for key, value in failure_context.items()
            }
            payload["reason_code"] = "notification.outbox_write_failed"
            await uow.process_events.append(
                ProcessEvent(
                    id=self._ids.new_id(),
                    event_type="notification.enqueue_failed",
                    actor_type="runtime",
                    actor_id=principal_id,
                    payload=payload,
                    derivation_key=f"notification.enqueue_failed:{dedupe_key}",
                    created_at=now,
                )
            )
            return False
        return stored is not None
