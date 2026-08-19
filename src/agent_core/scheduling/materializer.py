"""Atomic conversion of a due schedule into an ordinary durable run."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import NewEvent, ProcessEvent
from agent_core.domain.recurrence import RecurrenceCalculator
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunStatus
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    OnceCadence,
    Schedule,
    ScheduleAdmissionOutcome,
    ScheduleOccurrence,
    SchedulePauseReason,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.observability.schedules import ScheduleMetrics
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import (
    ScheduleCheckpointSeeder,
    ScheduleUnitOfWork,
    ScheduleUnitOfWorkFactory,
)
from agent_core.ports.schedules import (
    ScheduleAdmissionController,
    SchedulePrincipalDirectory,
)

WriteProbe = Callable[[str], None]


class ScheduleMaterializer:
    """Make one due occurrence durable in the schedule transaction."""

    def __init__(
        self,
        *,
        uow_factory: ScheduleUnitOfWorkFactory,
        principals: SchedulePrincipalDirectory,
        admission: ScheduleAdmissionController | None = None,
        clock: Clock,
        ids: IdFactory,
        seed_checkpoint: ScheduleCheckpointSeeder,
        write_probe: WriteProbe | None = None,
        metrics: ScheduleMetrics | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._principals = principals
        self._admission = admission
        self._clock = clock
        self._ids = ids
        self._seed_checkpoint = seed_checkpoint
        self._write_probe = write_probe or (lambda _boundary: None)
        self._metrics = metrics or ScheduleMetrics()

    async def materialize(self, schedule_id: UUID) -> ScheduleOccurrence | None:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            schedule = await uow.schedules.lock_due(schedule_id, now)
            if schedule is None:
                return await uow.schedule_occurrences.latest_at_or_before(schedule_id, now)

            owner = Principal(
                tenant_id=schedule.tenant_id,
                principal_id=schedule.principal_id,
                roles=set(),
                scopes=set(),
            )
            revision = await uow.schedules.get_revision(
                schedule.id, schedule.current_revision, owner
            )
            nominal = RecurrenceCalculator.latest_at_or_before(revision.cadence, now)
            if nominal is None or schedule.next_fire_at is None or nominal < schedule.next_fire_at:
                return None
            existing = await uow.schedule_occurrences.get_by_nominal(schedule.id, nominal)
            if existing is not None:
                return existing
            next_fire_at = RecurrenceCalculator.next_after(revision.cadence, now)
            await self._append_coalesced_misfires(
                uow,
                schedule,
                revision,
                schedule.next_fire_at,
                nominal,
                now,
            )

            latest_materialized = await uow.schedule_occurrences.latest_materialized(schedule.id)
            if latest_materialized is not None and latest_materialized.run_id is not None:
                linked_run = await uow.runs.get(latest_materialized.run_id, owner)
                if linked_run.status not in TERMINAL_RUN_STATUSES:
                    return await self._record_failure(
                        uow,
                        schedule,
                        revision,
                        nominal,
                        next_fire_at,
                        now,
                        OccurrenceDisposition.SKIPPED_OVERLAP,
                        "schedule.in_flight",
                        increment_failure=False,
                    )

            if now > nominal + timedelta(seconds=revision.misfire_grace_seconds):
                return await self._record_failure(
                    uow,
                    schedule,
                    revision,
                    nominal,
                    next_fire_at,
                    now,
                    OccurrenceDisposition.MISSED,
                    "schedule.misfire_grace_expired",
                )

            authority = await self._principals.current(schedule.tenant_id, schedule.principal_id)
            if authority is None:
                return await self._record_failure(
                    uow,
                    schedule,
                    revision,
                    nominal,
                    next_fire_at,
                    now,
                    OccurrenceDisposition.AUTHORIZATION_FAILED,
                    "schedule.principal_missing",
                )
            if not authority.enabled:
                return await self._record_failure(
                    uow,
                    schedule,
                    revision,
                    nominal,
                    next_fire_at,
                    now,
                    OccurrenceDisposition.AUTHORIZATION_FAILED,
                    "schedule.principal_disabled",
                    authority.authority_version,
                )
            if not revision.requested_scopes <= authority.principal.scopes:
                return await self._record_failure(
                    uow,
                    schedule,
                    revision,
                    nominal,
                    next_fire_at,
                    now,
                    OccurrenceDisposition.AUTHORIZATION_FAILED,
                    "schedule.scope_revoked",
                    authority.authority_version,
                )

            try:
                agent = await uow.agents.get_version(revision.agent_id, revision.agent_version)
            except NotFoundError:
                return await self._record_failure(
                    uow,
                    schedule,
                    revision,
                    nominal,
                    next_fire_at,
                    now,
                    OccurrenceDisposition.CONFIGURATION_FAILED,
                    "schedule.agent_version_missing",
                    authority.authority_version,
                )
            if agent.policy_profile != revision.policy_profile:
                return await self._record_failure(
                    uow,
                    schedule,
                    revision,
                    nominal,
                    next_fire_at,
                    now,
                    OccurrenceDisposition.CONFIGURATION_FAILED,
                    "schedule.policy_profile_mismatch",
                    authority.authority_version,
                )

            admission_controller = self._admission or uow.schedule_admission
            admission = await admission_controller.check(schedule.tenant_id, revision, now)
            if admission.outcome is ScheduleAdmissionOutcome.RETRY:
                return None
            if admission.outcome is ScheduleAdmissionOutcome.REJECT:
                assert admission.reason_code is not None
                return await self._record_failure(
                    uow,
                    schedule,
                    revision,
                    nominal,
                    next_fire_at,
                    now,
                    OccurrenceDisposition.MISSED,
                    admission.reason_code,
                    authority.authority_version,
                )

            session_id = self._ids.new_id()
            run_id = self._ids.new_id()
            occurrence = ScheduleOccurrence(
                id=self._ids.new_id(),
                schedule_id=schedule.id,
                schedule_revision=revision.revision,
                nominal_fire_at=nominal,
                disposition=OccurrenceDisposition.MATERIALIZED,
                session_id=session_id,
                run_id=run_id,
                authority_version=authority.authority_version,
                materialized_at=now,
                created_at=now,
            )
            await uow.schedule_occurrences.insert(occurrence)
            self._write_probe("occurrence")

            session = Session(
                id=session_id,
                tenant_id=schedule.tenant_id,
                principal_id=schedule.principal_id,
                agent_id=agent.id,
                agent_version=agent.version,
                status=SessionStatus.ACTIVE,
                title=revision.title,
                metadata={
                    "schedule_id": str(schedule.id),
                    "schedule_revision": revision.revision,
                    "schedule_occurrence_id": str(occurrence.id),
                    "nominal_fire_at": nominal.isoformat(),
                },
                created_at=now,
                updated_at=now,
            )
            await uow.sessions.create(session)
            self._write_probe("session")
            await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=None,
                    event_type="session.created",
                    payload_schema_version=2,
                    actor_type="scheduler",
                    actor_id=schedule.principal_id,
                    payload={"agent_id": str(agent.id), "title": revision.title},
                )
            )
            self._write_probe("session_event")

            deadline = now + timedelta(seconds=revision.run_timeout_seconds)
            run_limits = revision.limits.model_copy(update={"deadline_at": deadline}, deep=True)
            run = Run(
                id=run_id,
                session_id=session.id,
                tenant_id=schedule.tenant_id,
                principal_scopes=set(revision.requested_scopes),
                agent_id=agent.id,
                agent_version=agent.version,
                status=RunStatus.QUEUED,
                limits=run_limits,
                priority=10,
                scheduled_for=now,
                deadline_at=deadline,
                created_at=now,
                updated_at=now,
            )
            if uow.queue is None:
                await uow.runs.create(run)
            else:
                await uow.queue.enqueue(run, priority=run.priority, scheduled_for=now)
            self._write_probe("run")
            user_event = await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=run.id,
                    event_type="user.message.created",
                    actor_type="scheduler",
                    actor_id=schedule.principal_id,
                    payload={"content": revision.instruction},
                )
            )
            await uow.runs.set_seed_event_sequence(run.id, user_event.sequence)
            self._write_probe("instruction")
            await uow.events.append(
                NewEvent(
                    session_id=session.id,
                    run_id=run.id,
                    event_type="run.queued",
                    actor_type="scheduler",
                    actor_id=schedule.principal_id,
                    payload={
                        "run_id": str(run.id),
                        "priority": run.priority,
                        "schedule_id": str(schedule.id),
                        "schedule_revision": revision.revision,
                        "schedule_occurrence_id": str(occurrence.id),
                        "authority_version": authority.authority_version,
                    },
                )
            )
            self._write_probe("queued_event")
            run_principal = authority.principal.model_copy(
                update={"scopes": set(revision.requested_scopes)}, deep=True
            )
            await self._seed_checkpoint(uow, run, user_event.sequence, None, run_principal)
            self._write_probe("checkpoint")
            advanced = self._advanced_schedule(schedule, revision, next_fire_at, now, failed=False)
            await self._append_occurrence_event(uow, occurrence, schedule, advanced.state, now)
            self._write_probe("process_event")
            await uow.schedules.advance(schedule, advanced)
            self._write_probe("schedule")
            return occurrence

    async def _record_failure(
        self,
        uow: ScheduleUnitOfWork,
        schedule: Schedule,
        revision: ScheduleRevision,
        nominal: datetime,
        next_fire_at: datetime | None,
        now: datetime,
        disposition: OccurrenceDisposition,
        reason_code: str,
        authority_version: str | None = None,
        *,
        increment_failure: bool = True,
    ) -> ScheduleOccurrence:
        occurrence = ScheduleOccurrence(
            id=self._ids.new_id(),
            schedule_id=schedule.id,
            schedule_revision=revision.revision,
            nominal_fire_at=nominal,
            disposition=disposition,
            reason_code=reason_code,
            authority_version=authority_version,
            created_at=now,
        )
        await uow.schedule_occurrences.insert(occurrence)
        self._write_probe("occurrence")
        advanced = self._advanced_schedule(
            schedule, revision, next_fire_at, now, failed=increment_failure
        )
        await self._append_occurrence_event(uow, occurrence, schedule, advanced.state, now)
        self._write_probe("process_event")
        if (
            schedule.state is ScheduleState.ACTIVE
            and advanced.state is ScheduleState.PAUSED
            and advanced.pause_reason is SchedulePauseReason.FAILURE_LIMIT
        ):
            await uow.process_events.append(
                ProcessEvent(
                    id=self._ids.new_id(),
                    event_type="schedule.auto_paused",
                    actor_type="scheduler",
                    actor_id=schedule.principal_id,
                    payload={
                        "schedule_id": str(schedule.id),
                        "revision": schedule.current_revision,
                        "tenant_id": schedule.tenant_id,
                        "principal_id": schedule.principal_id,
                        "previous_state": schedule.state.value,
                        "next_state": advanced.state.value,
                        "occurrence_id": str(occurrence.id),
                        "run_id": None,
                        "event_time": now.isoformat(),
                    },
                    derivation_key=f"schedule.auto_paused:{occurrence.id}",
                    created_at=now,
                )
            )
            self._metrics.record_auto_pause()
        await uow.schedules.advance(schedule, advanced)
        self._write_probe("schedule")
        return occurrence

    async def _append_occurrence_event(
        self,
        uow: ScheduleUnitOfWork,
        occurrence: ScheduleOccurrence,
        schedule: Schedule,
        next_state: ScheduleState,
        now: datetime,
    ) -> None:
        suffix = {
            OccurrenceDisposition.MATERIALIZED: "materialized",
            OccurrenceDisposition.MISSED: "missed",
            OccurrenceDisposition.SKIPPED_OVERLAP: "skipped_overlap",
            OccurrenceDisposition.AUTHORIZATION_FAILED: "authorization_failed",
            OccurrenceDisposition.CONFIGURATION_FAILED: "configuration_failed",
        }[occurrence.disposition]
        await uow.process_events.append(
            ProcessEvent(
                id=self._ids.new_id(),
                event_type=f"schedule.occurrence.{suffix}",
                actor_type="scheduler",
                actor_id=schedule.principal_id,
                payload={
                    "schedule_id": str(schedule.id),
                    "schedule_revision": occurrence.schedule_revision,
                    "tenant_id": schedule.tenant_id,
                    "principal_id": schedule.principal_id,
                    "actor": "scheduler",
                    "event_time": now.isoformat(),
                    "previous_state": schedule.state.value,
                    "next_state": next_state.value,
                    "occurrence_id": str(occurrence.id),
                    "nominal_fire_at": occurrence.nominal_fire_at.isoformat(),
                    "disposition": occurrence.disposition.value,
                    "reason_code": occurrence.reason_code,
                    "session_id": (
                        None if occurrence.session_id is None else str(occurrence.session_id)
                    ),
                    "run_id": None if occurrence.run_id is None else str(occurrence.run_id),
                },
                derivation_key=f"schedule.occurrence:{occurrence.id}",
                created_at=now,
            )
        )
        self._metrics.record_occurrence(
            disposition=occurrence.disposition,
            nominal_fire_at=occurrence.nominal_fire_at,
            observed_at=now,
        )

    async def _append_coalesced_misfires(
        self,
        uow: ScheduleUnitOfWork,
        schedule: Schedule,
        revision: ScheduleRevision,
        first_nominal_at: datetime,
        last_nominal_at: datetime,
        now: datetime,
    ) -> None:
        count = RecurrenceCalculator.count_between(
            revision.cadence, first_nominal_at, last_nominal_at
        )
        if count <= 1:
            return
        await uow.process_events.append(
            ProcessEvent(
                id=self._ids.new_id(),
                event_type="schedule.misfires_coalesced",
                actor_type="scheduler",
                actor_id=schedule.principal_id,
                payload={
                    "schedule_id": str(schedule.id),
                    "schedule_revision": revision.revision,
                    "tenant_id": schedule.tenant_id,
                    "principal_id": schedule.principal_id,
                    "actor": "scheduler",
                    "event_time": now.isoformat(),
                    "first_nominal_at": first_nominal_at.isoformat(),
                    "last_nominal_at": last_nominal_at.isoformat(),
                    "count": count,
                },
                derivation_key=(
                    f"schedule.misfires_coalesced:{schedule.id}:"
                    f"{revision.revision}:{last_nominal_at.isoformat()}"
                ),
                created_at=now,
            )
        )
        self._metrics.record_misfires(
            count=count,
            outage_seconds=max(0.0, (last_nominal_at - first_nominal_at).total_seconds()),
        )

    @staticmethod
    def _advanced_schedule(
        schedule: Schedule,
        revision: ScheduleRevision,
        next_fire_at: datetime | None,
        now: datetime,
        *,
        failed: bool,
    ) -> Schedule:
        failures = schedule.consecutive_failures + (1 if failed else 0)
        if isinstance(revision.cadence, OnceCadence) or next_fire_at is None:
            return schedule.model_copy(
                update={
                    "state": ScheduleState.COMPLETED,
                    "pause_reason": None,
                    "next_fire_at": None,
                    "consecutive_failures": failures,
                    "updated_at": now,
                }
            )
        if failed and failures >= revision.max_consecutive_failures:
            return schedule.model_copy(
                update={
                    "state": ScheduleState.PAUSED,
                    "pause_reason": SchedulePauseReason.FAILURE_LIMIT,
                    "next_fire_at": next_fire_at,
                    "consecutive_failures": failures,
                    "updated_at": now,
                }
            )
        return schedule.model_copy(
            update={
                "state": ScheduleState.ACTIVE,
                "pause_reason": None,
                "next_fire_at": next_fire_at,
                "consecutive_failures": failures,
                "updated_at": now,
            }
        )
