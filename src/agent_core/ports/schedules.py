"""Persistence ports for schedule definitions and occurrence history."""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.schedules import (
    AuthoritySnapshot,
    OccurrenceCursor,
    Schedule,
    ScheduleAdmissionDecision,
    ScheduleCursor,
    ScheduledRunLink,
    ScheduleIdempotencyRecord,
    ScheduleOccurrence,
    ScheduleRevision,
)


class SchedulePrincipalDirectory(Protocol):
    async def current(self, tenant_id: str, principal_id: str) -> AuthoritySnapshot | None: ...


class ScheduleAdmissionController(Protocol):
    async def check(
        self, tenant_id: str, revision: ScheduleRevision, now: datetime
    ) -> ScheduleAdmissionDecision: ...


class ScheduleRepository(Protocol):
    async def create(self, schedule: Schedule, revision: ScheduleRevision) -> Schedule: ...

    async def get(self, schedule_id: UUID, principal: Principal) -> Schedule: ...

    async def lock(self, schedule_id: UUID, principal: Principal) -> Schedule: ...

    async def get_revision(
        self, schedule_id: UUID, revision: int, principal: Principal
    ) -> ScheduleRevision: ...

    async def list(
        self,
        principal: Principal,
        *,
        limit: int,
        cursor: ScheduleCursor | None = None,
    ) -> builtins.list[Schedule]: ...

    async def due(self, now: datetime, limit: int) -> builtins.list[UUID]: ...

    async def next_fire_at(self) -> datetime | None: ...

    async def lock_due(self, schedule_id: UUID, now: datetime) -> Schedule | None: ...

    async def advance(self, previous: Schedule, updated: Schedule) -> Schedule: ...

    async def replace(
        self,
        previous: Schedule,
        updated: Schedule,
        revision: ScheduleRevision | None = None,
    ) -> Schedule: ...


class ScheduleOccurrenceRepository(Protocol):
    async def insert(self, occurrence: ScheduleOccurrence) -> ScheduleOccurrence: ...

    async def get_by_nominal(
        self, schedule_id: UUID, nominal_fire_at: datetime
    ) -> ScheduleOccurrence | None: ...

    async def latest_at_or_before(
        self, schedule_id: UUID, instant: datetime
    ) -> ScheduleOccurrence | None: ...

    async def latest_materialized(self, schedule_id: UUID) -> ScheduleOccurrence | None: ...

    async def get_by_run(self, run_id: UUID) -> ScheduledRunLink | None: ...

    async def list(
        self,
        schedule_id: UUID,
        principal: Principal,
        *,
        limit: int,
        cursor: OccurrenceCursor | None = None,
    ) -> builtins.list[ScheduleOccurrence]: ...


class ScheduleIdempotencyRepository(Protocol):
    async def get(
        self, tenant_id: str, principal_id: str, key: str
    ) -> ScheduleIdempotencyRecord | None: ...

    async def create(self, record: ScheduleIdempotencyRecord) -> ScheduleIdempotencyRecord: ...
