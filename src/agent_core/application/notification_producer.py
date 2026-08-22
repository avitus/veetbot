"""Transactional notification production for existing run and schedule transitions."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from agent_core.domain.events import ProcessEvent
from agent_core.domain.notifications import (
    NOTIFICATION_TITLES,
    NewNotification,
    NotificationKind,
    NotificationPayload,
    approval_requested_key,
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
            NewNotification(
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
            NewNotification(
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
            NewNotification(
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
        )

    async def _enqueue_or_audit(
        self,
        uow: NotificationProductionUnitOfWork,
        notification: NewNotification,
    ) -> bool:
        try:
            stored = await uow.notification_outbox.enqueue(notification)
        except Exception:
            now = self._clock.now()
            await uow.process_events.append(
                ProcessEvent(
                    id=self._ids.new_id(),
                    event_type="notification.enqueue_failed",
                    actor_type="runtime",
                    actor_id=notification.principal_id,
                    payload={
                        "tenant_id": notification.tenant_id,
                        "principal_id": notification.principal_id,
                        "notification_id": str(notification.id),
                        "session_id": (
                            None
                            if notification.session_id is None
                            else str(notification.session_id)
                        ),
                        "run_id": (
                            None if notification.run_id is None else str(notification.run_id)
                        ),
                        "approval_id": (
                            None
                            if notification.approval_id is None
                            else str(notification.approval_id)
                        ),
                        "question_id": (
                            None
                            if notification.question_id is None
                            else str(notification.question_id)
                        ),
                        "schedule_id": (
                            None
                            if notification.schedule_id is None
                            else str(notification.schedule_id)
                        ),
                        "occurrence_id": (
                            None
                            if notification.occurrence_id is None
                            else str(notification.occurrence_id)
                        ),
                        "reason_code": "notification.outbox_write_failed",
                    },
                    derivation_key=f"notification.enqueue_failed:{notification.dedupe_key}",
                    created_at=now,
                )
            )
            return False
        return stored is not None
