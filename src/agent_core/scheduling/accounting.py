"""Idempotent post-run accounting for scheduled occurrences."""

from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.events import ProcessEvent
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, RunStatus
from agent_core.domain.schedules import SchedulePauseReason, ScheduleState
from agent_core.observability.schedules import ScheduleMetrics
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import ScheduleUnitOfWorkFactory


class ScheduleOutcomeAccountant:
    def __init__(
        self,
        *,
        uow_factory: ScheduleUnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        metrics: ScheduleMetrics | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._metrics = metrics or ScheduleMetrics()

    async def account(self, run_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            link = await uow.schedule_occurrences.get_by_run(run_id)
            if link is None:
                return False
            occurrence = link.occurrence
            derivation_key = f"schedule.run_accounted:{occurrence.id}"
            if await uow.process_events.get_by_derivation(derivation_key) is not None:
                return False
            owner = Principal(
                tenant_id=link.tenant_id,
                principal_id=link.principal_id,
                roles=set(),
                scopes=set(),
            )
            schedule = await uow.schedules.lock(occurrence.schedule_id, owner)
            if await uow.process_events.get_by_derivation(derivation_key) is not None:
                return False
            run = await uow.runs.get(run_id, owner)
            if run.status not in TERMINAL_RUN_STATUSES:
                return False
            revision = await uow.schedules.get_revision(
                schedule.id, occurrence.schedule_revision, owner
            )
            failures = schedule.consecutive_failures
            if run.status is RunStatus.COMPLETED:
                failures = 0
            elif run.status is RunStatus.FAILED:
                failures += 1
            auto_paused = (
                run.status is RunStatus.FAILED
                and failures >= revision.max_consecutive_failures
                and schedule.state is ScheduleState.ACTIVE
            )
            updated = schedule.model_copy(
                update={
                    "state": ScheduleState.PAUSED if auto_paused else schedule.state,
                    "pause_reason": (
                        SchedulePauseReason.FAILURE_LIMIT if auto_paused else schedule.pause_reason
                    ),
                    "consecutive_failures": failures,
                    "updated_at": self._clock.now(),
                }
            )
            if updated != schedule:
                await uow.schedules.replace(schedule, updated)
            await uow.process_events.append(
                ProcessEvent(
                    id=self._ids.new_id(),
                    event_type="schedule.run_accounted",
                    actor_type="scheduler",
                    actor_id=schedule.principal_id,
                    payload={
                        "schedule_id": str(schedule.id),
                        "schedule_revision": occurrence.schedule_revision,
                        "tenant_id": schedule.tenant_id,
                        "principal_id": schedule.principal_id,
                        "occurrence_id": str(occurrence.id),
                        "run_id": str(run.id),
                        "run_status": run.status.value,
                        "consecutive_failures": failures,
                        "event_time": self._clock.now().isoformat(),
                    },
                    derivation_key=derivation_key,
                    created_at=self._clock.now(),
                )
            )
            if auto_paused:
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
                            "next_state": ScheduleState.PAUSED.value,
                            "occurrence_id": str(occurrence.id),
                            "run_id": str(run.id),
                            "event_time": self._clock.now().isoformat(),
                        },
                        derivation_key=f"schedule.auto_paused:{occurrence.id}",
                        created_at=self._clock.now(),
                    )
                )
                self._metrics.record_auto_pause()
            self._metrics.record_terminal(
                tenant_id=schedule.tenant_id,
                status=run.status.value,
                duration_seconds=max(0.0, (run.updated_at - run.created_at).total_seconds()),
                cost=run.usage.cost,
                cancellation_seconds=(
                    None
                    if run.cancel_requested_at is None
                    else max(
                        0.0,
                        (run.updated_at - run.cancel_requested_at).total_seconds(),
                    )
                ),
                lease_reclaims=max(0, run.attempts - 1),
            )
            return True
