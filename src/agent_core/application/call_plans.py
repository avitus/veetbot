"""ADR-0221: advance call plans from each placed call's closed result."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from agent_core.domain.agents import Principal
from agent_core.domain.call_plans import (
    CALL_PLAN_STEP_EVENT,
    CallPlanAttempt,
    CallPlanStep,
    CallPlanStepKind,
    CallResult,
    call_plan_step_key,
    next_call_plan_step,
)
from agent_core.domain.calls import CallRecord
from agent_core.domain.events import ProcessEvent
from agent_core.domain.runs import TERMINAL_RUN_STATUSES
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    OnceCadence,
    Schedule,
    ScheduleCursor,
    ScheduleOccurrence,
    ScheduleState,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

_PAGE = 100
# Dispatch statuses while a call may still be ringing, connected or unreconciled.
_IN_FLIGHT = frozenset({"uncertain", "accepted", "active"})
_NOT_REACHED = {
    "no_answer": CallResult.NO_ANSWER,
    "busy": CallResult.BUSY,
    "voicemail": CallResult.VOICEMAIL,
    "failed": CallResult.FAILED,
}


class CallPlanSweep:
    """Choose each plan's next call from closed call results; never from call content.

    It runs in the maintenance role, which can read the call ledger. The
    scheduler cannot, so this sweep records the decided step as a process event
    and sets the schedule's next firing, which the scheduler then materializes.
    """

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        wake_scheduler: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._wake_scheduler = wake_scheduler

    async def run_once(self) -> int:
        waiting = await self._waiting_schedules()
        advanced = 0
        for schedule in waiting:
            if await self._advance(schedule):
                advanced += 1
        if advanced and self._wake_scheduler is not None:
            try:
                await self._wake_scheduler()
            except Exception:
                logger.warning("call_plan.scheduler_wakeup_failed", exc_info=True)
        return advanced

    async def _waiting_schedules(self) -> list[Schedule]:
        """Active schedules with no next firing: a call plan awaiting a result."""
        waiting: list[Schedule] = []
        cursor: ScheduleCursor | None = None
        async with self._uow_factory() as uow:
            while True:
                page = await uow.schedules.list(
                    self._principal,
                    limit=_PAGE,
                    cursor=cursor,
                    states=frozenset({ScheduleState.ACTIVE}),
                )
                waiting.extend(item for item in page if item.next_fire_at is None)
                if len(page) < _PAGE:
                    return waiting
                cursor = ScheduleCursor(updated_at=page[-1].updated_at, id=page[-1].id)

    async def _advance(self, candidate: Schedule) -> bool:
        owner = self._principal
        now = self._clock.now()
        async with self._uow_factory() as uow:
            revision = await uow.schedules.get_revision(
                candidate.id, candidate.current_revision, owner
            )
            plan = revision.call_plan
            if plan is None or not isinstance(revision.cadence, OnceCadence):
                return False
            schedule = await uow.schedules.lock(candidate.id, owner)
            if (
                schedule.state is not ScheduleState.ACTIVE
                or schedule.next_fire_at is not None
                or schedule.current_revision != revision.revision
            ):
                return False
            occurrences = sorted(
                (
                    item
                    for item in await uow.schedule_occurrences.list(schedule.id, owner, limit=_PAGE)
                    if item.disposition is OccurrenceDisposition.MATERIALIZED
                ),
                key=lambda item: item.nominal_fire_at,
            )
            if not occurrences:
                return False
            dispatches = await _dispatches(uow, owner)
            attempts = [
                await self._attempt(uow, schedule, index, occurrence, dispatches)
                for index, occurrence in enumerate(occurrences, start=1)
            ]
            if attempts[-1].kind is CallPlanStepKind.REPORT:
                return False
            step = next_call_plan_step(plan, revision.cadence.at, attempts, now)
            if step is None:
                return False
            key = call_plan_step_key(schedule.id, step.attempt)
            recorded = await uow.process_events.get_by_derivation(key)
            if recorded is None:
                await uow.process_events.append(
                    ProcessEvent(
                        id=self._ids.new_id(),
                        event_type=CALL_PLAN_STEP_EVENT,
                        actor_type="scheduler",
                        actor_id=schedule.principal_id,
                        payload={
                            "schedule_id": str(schedule.id),
                            "schedule_revision": revision.revision,
                            "tenant_id": schedule.tenant_id,
                            "principal_id": schedule.principal_id,
                            "event_time": now.isoformat(),
                            "step": step.model_dump(mode="json"),
                        },
                        derivation_key=key,
                        created_at=now,
                    )
                )
            else:
                step = CallPlanStep.model_validate(recorded.payload["step"])
            await uow.schedules.replace(
                schedule,
                schedule.model_copy(update={"next_fire_at": step.fire_at, "updated_at": now}),
            )
        return True

    async def _attempt(
        self,
        uow: RepositoryUnitOfWork,
        schedule: Schedule,
        index: int,
        occurrence: ScheduleOccurrence,
        dispatches: list[CallRecord],
    ) -> CallPlanAttempt:
        kind = CallPlanStepKind.FIRST
        if index > 1:
            decided = await uow.process_events.get_by_derivation(
                call_plan_step_key(schedule.id, index)
            )
            kind = (
                CallPlanStepKind.RETRY
                if decided is None
                else CallPlanStep.model_validate(decided.payload["step"]).kind
            )
        result, ended_at = await self._result(uow, occurrence, dispatches)
        assert occurrence.materialized_at is not None
        return CallPlanAttempt(
            kind=kind,
            nominal_fire_at=occurrence.nominal_fire_at,
            materialized_at=occurrence.materialized_at,
            result=result,
            ended_at=ended_at,
        )

    async def _result(
        self,
        uow: RepositoryUnitOfWork,
        occurrence: ScheduleOccurrence,
        dispatches: list[CallRecord],
    ) -> tuple[CallResult, datetime | None]:
        """Read only the closed status the call ledger keeps for each dispatch."""
        if occurrence.run_id is None:
            return CallResult.NO_CALL, None
        run = await uow.runs.get(occurrence.run_id, self._principal)
        if run.status not in TERMINAL_RUN_STATUSES:
            return CallResult.PENDING, None
        rows = sorted(
            (row for row in dispatches if row.payload.get("run_id") == str(run.id)),
            key=lambda row: row.updated_at,
        )
        if not rows:
            return CallResult.NO_CALL, None
        statuses = [str(row.payload.get("status")) for row in rows]
        for row, status in zip(rows, statuses, strict=True):
            if status == "completed":
                return CallResult.ANSWERED, await self._ended_at(uow, row)
        if any(status in _IN_FLIGHT for status in statuses):
            return CallResult.PENDING, None
        for status in reversed(statuses):
            if status in _NOT_REACHED:
                return _NOT_REACHED[status], None
        if all(status == "not_dispatched" for status in statuses):
            return CallResult.NO_CALL, None
        return CallResult.UNKNOWN, None

    async def _ended_at(self, uow: RepositoryUnitOfWork, dispatch: CallRecord) -> datetime:
        """When an answered call ended: its start plus its length, else when it settled."""
        provider_id = dispatch.payload.get("provider_call_id")
        call = (
            await uow.calls.get(self._principal, "call", provider_id)
            if isinstance(provider_id, str)
            else None
        )
        if call is not None:
            started = call.payload.get("created_at")
            minutes = call.payload.get("duration_minutes")
            if isinstance(started, str) and isinstance(minutes, int | float):
                try:
                    return datetime.fromisoformat(started) + timedelta(minutes=float(minutes))
                except ValueError:
                    pass
        return dispatch.updated_at


async def _dispatches(uow: RepositoryUnitOfWork, owner: Principal) -> list[CallRecord]:
    rows: list[CallRecord] = []
    after: str | None = None
    while True:
        page = await uow.calls.list(owner, "dispatch", after=after, limit=_PAGE)
        rows.extend(page)
        if len(page) < _PAGE:
            return rows
        after = page[-1].key
