"""Tenant admission adapters for ordinary runs originating on a surface."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.domain.surfaces import SurfaceAdmissionDecision, SurfaceLimits


class AllowSurfaceAdmissionController:
    async def check(
        self,
        tenant_id: str,
        reservation: Decimal | None,
        now: datetime,
    ) -> SurfaceAdmissionDecision:
        del tenant_id, reservation, now
        return SurfaceAdmissionDecision(allowed=True)


class PostgresSurfaceAdmissionController:
    """Serialize and account surface-run reservations for one tenant."""

    def __init__(self, session: AsyncSession, limits: SurfaceLimits) -> None:
        self._session = session
        self._limits = limits

    async def check(
        self,
        tenant_id: str,
        reservation: Decimal | None,
        now: datetime,
    ) -> SurfaceAdmissionDecision:
        now_utc = now.astimezone(UTC)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('surface:' || :tenant_id, 0))"),
            {"tenant_id": tenant_id},
        )
        row = (
            await self._session.execute(
                text(
                    "SELECT "
                    "count(*) FILTER (WHERE r.status NOT IN "
                    "('COMPLETED','FAILED','CANCELLED')) AS active_count, "
                    "coalesce(sum(CASE WHEN r.created_at >= :day_start THEN "
                    "CASE WHEN r.status NOT IN ('COMPLETED','FAILED','CANCELLED') "
                    "THEN coalesce((r.limits->>'max_cost')::numeric, 0) "
                    "ELSE (r.usage->>'cost')::numeric END ELSE 0 END), 0) AS day_cost, "
                    "coalesce(sum(CASE WHEN r.created_at >= :month_start THEN "
                    "CASE WHEN r.status NOT IN ('COMPLETED','FAILED','CANCELLED') "
                    "THEN coalesce((r.limits->>'max_cost')::numeric, 0) "
                    "ELSE (r.usage->>'cost')::numeric END ELSE 0 END), 0) AS month_cost "
                    "FROM runs r WHERE r.tenant_id = :tenant_id AND EXISTS ("
                    "SELECT 1 FROM events e WHERE e.run_id = r.id "
                    "AND e.event_type = 'run.queued' "
                    "AND e.payload->'origin'->>'surface_id' IS NOT NULL)"
                ),
                {
                    "tenant_id": tenant_id,
                    "day_start": now_utc.replace(hour=0, minute=0, second=0, microsecond=0),
                    "month_start": now_utc.replace(
                        day=1, hour=0, minute=0, second=0, microsecond=0
                    ),
                },
            )
        ).one()
        if int(row.active_count) >= self._limits.max_active_runs_per_tenant:
            return SurfaceAdmissionDecision(
                allowed=False,
                reason_code="surface.concurrency_limit",
            )
        if reservation is None:
            return SurfaceAdmissionDecision(
                allowed=False,
                reason_code="surface.cost_reservation_missing",
            )
        if Decimal(row.day_cost) + reservation > self._limits.daily_cost:
            return SurfaceAdmissionDecision(
                allowed=False,
                reason_code="surface.daily_cost_limit",
            )
        if Decimal(row.month_cost) + reservation > self._limits.monthly_cost:
            return SurfaceAdmissionDecision(
                allowed=False,
                reason_code="surface.monthly_cost_limit",
            )
        return SurfaceAdmissionDecision(allowed=True)
