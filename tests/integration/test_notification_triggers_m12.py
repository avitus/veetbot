"""PostgreSQL atomicity proof for notification-producing transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.persistence import notifications as notification_adapters
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.application.notification_producer import NotificationProducer
from agent_core.bootstrap import build
from agent_core.domain.notifications import NotificationStatus
from agent_core.domain.runs import (
    FailureReason,
    OutcomeKind,
    RunCheckpoint,
    RunFailure,
    RunOutcome,
    RunStatus,
)
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    OnceCadence,
    Schedule,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.runtime.executor import _FinalizationContext, _finalize_once
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from agent_core.scheduling.materializer import ScheduleMaterializer
from tests.contract.support import agent, run
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 22, 21, tzinfo=UTC)


class _InjectedCrashError(Exception):
    pass


async def _running_context(composition, *, crash_at: str | None = None):  # type: ignore[no-untyped-def]
    session_id = uuid4()
    run_id = uuid4()
    principal = composition.principal
    session = Session(
        id=session_id,
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        agent_id=agent().id,
        agent_version=agent().version,
        status=SessionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    running = run(status=RunStatus.RUNNING).model_copy(
        update={"id": run_id, "session_id": session_id, "tenant_id": principal.tenant_id}
    )
    async with composition.uow_factory() as uow:
        await uow.sessions.create(session)
        await uow.runs.create(running)

    def probe(boundary: str) -> None:
        if boundary == crash_at:
            raise _InjectedCrashError(boundary)

    return _FinalizationContext(
        run=running,
        checkpoint=RunCheckpoint(
            run_id=run_id,
            version=0,
            status=RunStatus.RUNNING,
            created_at=NOW,
        ),
        uow_factory=composition.uow_factory,
        lease=None,
        clock=composition.clock,
        ids=composition.ids,
        token=RunCancellationToken(composition.clock, None),
        principal=principal,
        notification_producer=NotificationProducer(
            clock=composition.clock,
            ids=composition.ids,
        ),
        finalization_write_probe=probe,
    )


def _failure() -> RunOutcome:
    return RunOutcome(
        kind=OutcomeKind.FAILED,
        failure=RunFailure(
            reason=FailureReason.INTERNAL_ERROR,
            error_class="InjectedFailure",
            message="sensitive failure detail remains in the run event only",
            occurred_at=NOW,
        ),
    )


async def _create_schedule(composition, *, missed: bool):  # type: ignore[no-untyped-def]
    schedule_id = uuid4()
    nominal = NOW - timedelta(minutes=2) if missed else NOW
    schedule = Schedule(
        id=schedule_id,
        tenant_id=composition.principal.tenant_id,
        principal_id=composition.principal.principal_id,
        state=ScheduleState.ACTIVE,
        current_revision=1,
        next_fire_at=nominal,
        created_at=nominal,
        updated_at=nominal,
    )
    revision = ScheduleRevision(
        schedule_id=schedule_id,
        revision=1,
        title="Schedule title is never copied",
        instruction="Schedule instruction is never copied",
        agent_id=agent().id,
        agent_version=agent().version,
        policy_profile=agent().policy_profile,
        requested_scopes=frozenset(),
        limits=run().limits.model_copy(update={"max_cost": Decimal("1")}),
        run_timeout_seconds=60,
        cadence=OnceCadence(at=nominal),
        timezone=None,
        misfire_grace_seconds=30,
        max_consecutive_failures=3,
        created_by_principal_id=composition.principal.principal_id,
        created_at=nominal,
    )
    async with composition.uow_factory() as uow:
        await uow.agents.put(agent())
        await uow.schedules.create(schedule, revision)
    return schedule


def _materializer(composition, *, crash_at: str | None = None):  # type: ignore[no-untyped-def]
    def probe(boundary: str) -> None:
        if boundary == crash_at:
            raise _InjectedCrashError(boundary)

    return ScheduleMaterializer(
        uow_factory=composition.uow_factory,
        principals=StaticSchedulePrincipalDirectory(composition.principal),
        admission=AllowScheduleAdmissionController(),
        clock=composition.clock,
        ids=composition.ids,
        seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        write_probe=probe,
        notification_producer=NotificationProducer(
            clock=composition.clock,
            ids=composition.ids,
        ),
    )


async def _assert_terminal_notification_atomic() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        for boundary in ("terminal_event", "notification", "checkpoint", "run"):
            context = await _running_context(composition, crash_at=boundary)
            with pytest.raises(_InjectedCrashError, match=boundary):
                await _finalize_once(context, _failure())

            async with composition.uow_factory() as uow:
                persisted = await uow.runs.get(context.run.id, composition.principal)
                assert persisted.status is RunStatus.RUNNING
                assert (
                    await uow.events.list_after(context.run.session_id, 0, composition.principal)
                    == []
                )
                assert not any(
                    row.run_id == context.run.id
                    for row in await uow.notification_outbox.list(composition.principal, limit=100)
                )

            retry = await _running_context(composition)
            retry.run = context.run
            retry.checkpoint = context.checkpoint.model_copy(
                update={"version": 0, "status": RunStatus.RUNNING, "last_event_sequence": 0}
            )
            await _finalize_once(retry, _failure())
            async with composition.uow_factory() as uow:
                persisted = await uow.runs.get(context.run.id, composition.principal)
                assert persisted.status is RunStatus.FAILED
                rows = await uow.notification_outbox.list(composition.principal, limit=100)
                assert sum(row.run_id == context.run.id for row in rows) == 1


async def _assert_outbox_savepoint_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = notification_adapters.new_notification_values  # type: ignore[attr-defined]

    def invalid_values(notification):  # type: ignore[no-untyped-def]
        values = original(notification)
        values["kind"] = "not-a-declared-kind"
        return values

    monkeypatch.setattr(notification_adapters, "new_notification_values", invalid_values)
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        context = await _running_context(composition)
        await _finalize_once(context, _failure())

        async with composition.uow_factory() as uow:
            persisted = await uow.runs.get(context.run.id, composition.principal)
            assert persisted.status is RunStatus.FAILED
            events = await uow.events.list_after(context.run.session_id, 0, composition.principal)
            assert [event.event_type for event in events] == ["run.failed"]
            assert not any(
                row.run_id == context.run.id
                for row in await uow.notification_outbox.list(composition.principal, limit=100)
            )
            failures = await uow.process_events.list("notification.enqueue_failed")
            [audit] = [
                event for event in failures if event.payload["run_id"] == str(context.run.id)
            ]
            assert audit.payload["reason_code"] == "notification.outbox_write_failed"
            assert "sensitive failure detail" not in audit.model_dump_json()
            assert "not-a-declared-kind" not in audit.model_dump_json()


async def test_session_erasure_deletes_pending_notifications_and_keeps_settled_audit() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        context = await _running_context(composition)
        producer = context.notification_producer
        assert producer is not None
        pending_run = context.run.model_copy(update={"id": uuid4()})
        async with composition.uow_factory() as uow:
            assert await producer.for_run_transition(
                uow,
                run=context.run,
                principal_id=composition.principal.principal_id,
                status=RunStatus.FAILED,
            )
            assert await producer.for_run_transition(
                uow,
                run=pending_run,
                principal_id=composition.principal.principal_id,
                status=RunStatus.FAILED,
            )
            rows = await uow.notification_outbox.list(composition.principal, limit=10)
            settled = next(row for row in rows if row.run_id == context.run.id)
            await uow.notification_outbox.settle(
                settled.id,
                NotificationStatus.DISPATCHED,
                None,
            )
            await uow.runs.transition(
                context.run.id,
                RunStatus.RUNNING,
                RunStatus.FAILED,
            )

        async with composition.uow_factory() as uow:
            assert await uow.session_deletions.delete(
                context.run.session_id,
                composition.principal,
                NOW,
            )
            rows = await uow.notification_outbox.list(composition.principal, limit=10)
            assert [row.run_id for row in rows] == [context.run.id]
            assert rows[0].status is NotificationStatus.DISPATCHED


async def _assert_schedule_skip_notification_atomic() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        for boundary in ("occurrence", "process_event", "notification", "schedule"):
            schedule = await _create_schedule(composition, missed=True)
            with pytest.raises(_InjectedCrashError, match=boundary):
                await _materializer(composition, crash_at=boundary).materialize(schedule.id)

            async with composition.uow_factory() as uow:
                assert (
                    await uow.schedule_occurrences.list(
                        schedule.id, composition.principal, limit=10
                    )
                    == []
                )
                assert not any(
                    row.schedule_id == schedule.id
                    for row in await uow.notification_outbox.list(composition.principal, limit=100)
                )

            occurrence = await _materializer(composition).materialize(schedule.id)
            assert occurrence is not None
            assert occurrence.disposition is OccurrenceDisposition.MISSED
            async with composition.uow_factory() as uow:
                rows = await uow.notification_outbox.list(composition.principal, limit=100)
                assert sum(row.occurrence_id == occurrence.id for row in rows) == 1


async def _assert_schedule_accounting_notification_atomic() -> None:
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        for boundary in ("schedule", "process_event", "notification"):
            schedule = await _create_schedule(composition, missed=False)
            occurrence = await _materializer(composition).materialize(schedule.id)
            assert occurrence is not None and occurrence.run_id is not None
            async with composition.uow_factory() as uow:
                await uow.runs.transition(
                    occurrence.run_id,
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                )
                await uow.runs.transition(
                    occurrence.run_id,
                    RunStatus.RUNNING,
                    RunStatus.FAILED,
                )

            def probe(actual: str, expected: str = boundary) -> None:
                if actual == expected:
                    raise _InjectedCrashError(actual)

            producer = NotificationProducer(clock=composition.clock, ids=composition.ids)
            accountant = ScheduleOutcomeAccountant(
                uow_factory=composition.uow_factory,
                clock=composition.clock,
                ids=composition.ids,
                notification_producer=producer,
                write_probe=probe,
            )
            with pytest.raises(_InjectedCrashError, match=boundary):
                await accountant.account(occurrence.run_id)

            async with composition.uow_factory() as uow:
                current = await uow.schedules.get(schedule.id, composition.principal)
                assert current.consecutive_failures == 0
                assert (
                    await uow.process_events.get_by_derivation(
                        f"schedule.run_accounted:{occurrence.id}"
                    )
                    is None
                )
                assert not any(
                    row.occurrence_id == occurrence.id
                    for row in await uow.notification_outbox.list(composition.principal, limit=100)
                )

            retry = ScheduleOutcomeAccountant(
                uow_factory=composition.uow_factory,
                clock=composition.clock,
                ids=composition.ids,
                notification_producer=producer,
            )
            assert await retry.account(occurrence.run_id)
            async with composition.uow_factory() as uow:
                rows = await uow.notification_outbox.list(composition.principal, limit=100)
                assert sum(row.occurrence_id == occurrence.id for row in rows) == 1


@pytest.mark.parametrize(
    "producer_path",
    ("terminal", "savepoint_failure", "schedule_skip", "schedule_accounting"),
)
async def test_postgres_notification_enqueue_is_atomic_for_every_producer(
    producer_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if producer_path == "terminal":
        await _assert_terminal_notification_atomic()
    elif producer_path == "savepoint_failure":
        await _assert_outbox_savepoint_failure(monkeypatch)
    elif producer_path == "schedule_skip":
        await _assert_schedule_skip_notification_atomic()
    else:
        assert producer_path == "schedule_accounting"
        await _assert_schedule_accounting_notification_atomic()
