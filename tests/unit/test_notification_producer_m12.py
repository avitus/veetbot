"""Closed transition catalog and failure-audit behavior for notifications."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.notification_producer import NotificationProducer
from agent_core.domain.notifications import NotificationKind, NotificationStatus
from agent_core.domain.runs import RunLimits, RunStatus
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    OnceCadence,
    Schedule,
    ScheduleOccurrence,
    ScheduleRevision,
    ScheduleState,
)
from tests.contract.support import (
    AGENT_ID,
    NOW,
    RUN_ID,
    SESSION_ID,
    memory_uow_factory,
    principal,
    run,
)

SCHEDULE_ID = UUID(int=401)
OCCURRENCE_ID = UUID(int=402)


def _schedule() -> Schedule:
    return Schedule(
        id=SCHEDULE_ID,
        tenant_id="tenant-a",
        principal_id="principal-a",
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _revision() -> ScheduleRevision:
    return ScheduleRevision(
        schedule_id=SCHEDULE_ID,
        revision=1,
        title="Sensitive title never copied",
        instruction="Sensitive instruction never copied",
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        policy_profile="default",
        requested_scopes=frozenset(),
        limits=RunLimits(
            max_steps=4,
            max_model_calls=4,
            max_tool_calls=4,
            max_cost=Decimal("1"),
        ),
        run_timeout_seconds=60,
        cadence=OnceCadence(at=NOW),
        timezone=None,
        misfire_grace_seconds=60,
        max_consecutive_failures=3,
        created_by_principal_id="principal-a",
        created_at=NOW,
    )


def _occurrence(
    disposition: OccurrenceDisposition,
    *,
    with_run: bool = False,
) -> ScheduleOccurrence:
    return ScheduleOccurrence(
        id=OCCURRENCE_ID,
        schedule_id=SCHEDULE_ID,
        schedule_revision=1,
        nominal_fire_at=NOW,
        disposition=disposition,
        session_id=SESSION_ID if with_run else None,
        run_id=RUN_ID if with_run else None,
        reason_code=(None if with_run else "schedule.test_reason"),
        authority_version="authority-v1" if with_run else None,
        materialized_at=NOW if with_run else None,
        created_at=NOW,
    )


async def test_exact_transition_catalog_builds_content_free_deduplicated_rows() -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    approval_id = UUID(int=410)
    question_id = UUID(int=411)
    running = run(status=RunStatus.RUNNING)
    scheduled = _schedule()
    revision = _revision()

    async with factory() as uow:
        await uow.schedules.create(scheduled, revision)
        assert await producer.for_run_transition(
            uow,
            run=running,
            principal_id="principal-a",
            status=RunStatus.WAITING_FOR_APPROVAL,
            approval_id=approval_id,
            approval_expires_at=NOW + timedelta(minutes=10),
        )
        assert await producer.for_run_transition(
            uow,
            run=running,
            principal_id="principal-a",
            status=RunStatus.WAITING_FOR_USER,
            question_id=question_id,
        )
        assert await producer.for_run_transition(
            uow,
            run=running,
            principal_id="principal-a",
            status=RunStatus.FAILED,
        )
        assert not await producer.for_run_transition(
            uow,
            run=running,
            principal_id="principal-a",
            status=RunStatus.COMPLETED,
        )
        accounted = _occurrence(OccurrenceDisposition.MATERIALIZED, with_run=True)
        assert await producer.for_schedule_run_accounted(
            uow,
            schedule=scheduled,
            occurrence=accounted,
            run=running.model_copy(update={"status": RunStatus.COMPLETED}),
        )
        for offset, disposition in enumerate(
            (
                OccurrenceDisposition.MISSED,
                OccurrenceDisposition.SKIPPED_OVERLAP,
                OccurrenceDisposition.AUTHORIZATION_FAILED,
                OccurrenceDisposition.CONFIGURATION_FAILED,
            ),
            start=1,
        ):
            occurrence = _occurrence(disposition).model_copy(
                update={"id": UUID(int=OCCURRENCE_ID.int + offset)}
            )
            assert await producer.for_schedule_occurrence(
                uow,
                schedule=scheduled,
                occurrence=occurrence,
            )
        assert not await producer.for_schedule_occurrence(
            uow,
            schedule=scheduled,
            occurrence=accounted,
        )

        rows = await uow.notification_outbox.list(principal(), limit=20)
        assert {row.kind for row in rows} == {
            NotificationKind.APPROVAL_REQUESTED,
            NotificationKind.QUESTION_ASKED,
            NotificationKind.RUN_FAILED,
            NotificationKind.SCHEDULE_RUN_FINISHED,
            NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED,
        }
        assert len(rows) == 8
        assert all(row.status is NotificationStatus.PENDING for row in rows)
        assert all("Sensitive" not in row.payload.model_dump_json() for row in rows)

        assert not await producer.for_run_transition(
            uow,
            run=running,
            principal_id="principal-a",
            status=RunStatus.FAILED,
        )
        assert len(await uow.notification_outbox.list(principal(), limit=20)) == 8


class _FailingOutbox:
    async def enqueue(self, notification):  # type: ignore[no-untyped-def]
        del notification
        raise RuntimeError("secret-provider-detail")


async def test_enqueue_failure_is_audited_without_leaking_exception_text() -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    async with factory() as uow:
        uow.notification_outbox = _FailingOutbox()  # type: ignore[assignment]
        assert not await producer.for_run_transition(
            uow,
            run=run(status=RunStatus.RUNNING),
            principal_id="principal-a",
            status=RunStatus.FAILED,
        )
        [event] = await uow.process_events.list("notification.enqueue_failed")
        assert event.payload["reason_code"] == "notification.outbox_write_failed"
        assert "secret-provider-detail" not in event.model_dump_json()


async def test_notification_validation_failure_is_audited_without_breaking_transition() -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    approval_id = UUID(int=499)

    async with factory() as uow:
        assert not await producer.for_run_transition(
            uow,
            run=run(status=RunStatus.RUNNING),
            principal_id="principal-a",
            status=RunStatus.WAITING_FOR_APPROVAL,
            approval_id=approval_id,
            approval_expires_at=NOW - timedelta(seconds=1),
        )
        [event] = await uow.process_events.list("notification.enqueue_failed")
        assert event.payload["approval_id"] == str(approval_id)
        assert event.payload["reason_code"] == "notification.outbox_write_failed"
        assert await uow.notification_outbox.list(principal(), limit=10) == []


async def test_required_trigger_identifiers_fail_closed() -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    async with factory() as uow:
        with pytest.raises(ValueError, match="approval"):
            await producer.for_run_transition(
                uow,
                run=run(status=RunStatus.RUNNING),
                principal_id="principal-a",
                status=RunStatus.WAITING_FOR_APPROVAL,
            )
        with pytest.raises(ValueError, match="question"):
            await producer.for_run_transition(
                uow,
                run=run(status=RunStatus.RUNNING),
                principal_id="principal-a",
                status=RunStatus.WAITING_FOR_USER,
            )
