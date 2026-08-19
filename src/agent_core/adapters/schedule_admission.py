"""Development admission adapter for scheduled materialization."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.domain.schedules import (
    ScheduleAdmissionDecision,
    ScheduleAdmissionLimits,
    ScheduleAdmissionOutcome,
    ScheduleRevision,
)
from agent_core.observability.schedules import ScheduleMetrics


class AllowScheduleAdmissionController:
    async def check(
        self, tenant_id: str, revision: ScheduleRevision, now: datetime
    ) -> ScheduleAdmissionDecision:
        return ScheduleAdmissionDecision(outcome=ScheduleAdmissionOutcome.ALLOW)


class PostgresScheduleAdmissionController:
    def __init__(
        self,
        session: AsyncSession,
        limits: ScheduleAdmissionLimits,
        metrics: ScheduleMetrics | None = None,
    ) -> None:
        self._session = session
        self._limits = limits
        self._metrics = metrics or ScheduleMetrics()

    async def check(
        self, tenant_id: str, revision: ScheduleRevision, now: datetime
    ) -> ScheduleAdmissionDecision:
        now_utc = now.astimezone(UTC)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant_id, 0))"),
            {"tenant_id": tenant_id},
        )
        row = (
            await self._session.execute(
                text(
                    "SELECT "
                    "count(*) FILTER (WHERE r.status NOT IN "
                    "('COMPLETED','FAILED','CANCELLED')) AS active_count, "
                    "count(*) FILTER (WHERE o.materialized_at >= :minute_start) "
                    "AS minute_count, "
                    "coalesce(sum(CASE WHEN r.created_at >= :day_start THEN "
                    "CASE WHEN r.status NOT IN ('COMPLETED','FAILED','CANCELLED') "
                    "THEN (r.limits->>'max_cost')::numeric "
                    "ELSE (r.usage->>'cost')::numeric END ELSE 0 END), 0) AS day_cost, "
                    "coalesce(sum(CASE WHEN r.created_at >= :month_start THEN "
                    "CASE WHEN r.status NOT IN ('COMPLETED','FAILED','CANCELLED') "
                    "THEN (r.limits->>'max_cost')::numeric "
                    "ELSE (r.usage->>'cost')::numeric END ELSE 0 END), 0) AS month_cost "
                    "FROM schedule_occurrences o "
                    "JOIN schedules s ON s.id = o.schedule_id "
                    "LEFT JOIN runs r ON r.id = o.run_id "
                    "WHERE s.tenant_id = :tenant_id "
                    "AND o.disposition = 'MATERIALIZED'"
                ),
                {
                    "tenant_id": tenant_id,
                    "minute_start": now_utc - timedelta(minutes=1),
                    "day_start": now_utc.replace(hour=0, minute=0, second=0, microsecond=0),
                    "month_start": now_utc.replace(
                        day=1, hour=0, minute=0, second=0, microsecond=0
                    ),
                },
            )
        ).one()
        self._metrics.record_admission(
            tenant_id=tenant_id,
            active_runs=int(row.active_count),
            day_cost=Decimal(row.day_cost),
            month_cost=Decimal(row.month_cost),
            day_ceiling=self._limits.daily_cost,
            month_ceiling=self._limits.monthly_cost,
        )
        if int(row.active_count) >= self._limits.max_active_runs_per_tenant:
            return ScheduleAdmissionDecision(
                outcome=ScheduleAdmissionOutcome.RETRY,
                reason_code="schedule.concurrency_limit",
            )
        if int(row.minute_count) >= self._limits.max_materializations_per_minute:
            return ScheduleAdmissionDecision(
                outcome=ScheduleAdmissionOutcome.REJECT,
                reason_code="schedule.rate_limit",
            )
        reservation = revision.limits.max_cost
        if reservation is None:
            return ScheduleAdmissionDecision(
                outcome=ScheduleAdmissionOutcome.REJECT,
                reason_code="schedule.cost_reservation_missing",
            )
        if Decimal(row.day_cost) + reservation > self._limits.daily_cost:
            return ScheduleAdmissionDecision(
                outcome=ScheduleAdmissionOutcome.REJECT,
                reason_code="schedule.daily_cost_limit",
            )
        if Decimal(row.month_cost) + reservation > self._limits.monthly_cost:
            return ScheduleAdmissionDecision(
                outcome=ScheduleAdmissionOutcome.REJECT,
                reason_code="schedule.monthly_cost_limit",
            )
        return ScheduleAdmissionDecision(outcome=ScheduleAdmissionOutcome.ALLOW)
