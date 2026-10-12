"""Owner-serialized semantic cursors and durable, content-free spend receipts."""

from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.reconsolidation_sources import lock_owner
from agent_core.adapters.persistence.sqlalchemy_models import SemanticJobRow, SemanticSpendRow
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.semantic_memory import SemanticBounds
from agent_core.domain.semantic_work import (
    QUANTUM,
    EmbeddingConfig,
    SemanticJob,
    SemanticSpend,
    check_cursors,
    claim_job,
    finish_job,
    require_job,
    settled_spend,
)
from agent_core.ports.determinism import IdFactory


class PostgresSemanticWorkStore:
    def __init__(self, session: AsyncSession, ids: IdFactory) -> None:
        self._session = session
        self._ids = ids

    async def _job(self, principal: Principal) -> SemanticJob | None:
        await lock_owner(self._session, principal)
        row = await self._session.scalar(
            select(SemanticJobRow)
            .where(
                SemanticJobRow.tenant_id == principal.tenant_id,
                SemanticJobRow.principal_id == principal.principal_id,
            )
            .execution_options(populate_existing=True)
        )
        return SemanticJob.model_validate(row.payload) if row else None

    async def _save(self, value: SemanticJob) -> None:
        statement = pg_insert(SemanticJobRow).values(
            tenant_id=value.tenant_id,
            principal_id=value.principal_id,
            payload=value.model_dump(mode="json"),
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "principal_id"],
                set_={"payload": statement.excluded.payload},
            )
        )

    async def _spend(self, principal: Principal, spend_id: UUID) -> SemanticSpendRow | None:
        row = await self._session.scalar(
            select(SemanticSpendRow)
            .where(
                SemanticSpendRow.tenant_id == principal.tenant_id,
                SemanticSpendRow.principal_id == principal.principal_id,
                SemanticSpendRow.id == spend_id,
            )
            .execution_options(populate_existing=True)
        )

        return row

    async def claim(
        self, principal: Principal, config: EmbeddingConfig, bounds: SemanticBounds, now: datetime
    ) -> SemanticJob | None:
        old = await self._job(principal)
        value = claim_job(
            old,
            tenant=principal.tenant_id,
            owner=principal.principal_id,
            config_key=config.key,
            full_bound=bounds.creation,
            change_bound=bounds.change,
            token=self._ids.new_id(),
            now=now,
        )
        if value is not None:
            if old is not None and old.request_id is not None:
                row = await self._spend(principal, old.request_id)
                if row is not None:
                    spend = SemanticSpend.model_validate(row.payload)
                    if spend.state == "reserved":
                        row.payload = settled_spend(spend, None).model_dump(mode="json")
            await self._save(value)
        return value

    async def current(self, principal: Principal, token: UUID, now: datetime) -> SemanticJob:
        return require_job(await self._job(principal), token, now)

    async def reserve(
        self,
        principal: Principal,
        token: UUID,
        config: EmbeddingConfig,
        digest: str,
        maximum: Decimal,
        full_cursor: int,
        change_cursor: int,
        now: datetime,
    ) -> SemanticSpend | None:
        if not maximum.is_finite():
            return None
        maximum = maximum.quantize(QUANTUM, rounding=ROUND_CEILING)
        job = await self.current(principal, token, now)
        check_cursors(job, full_cursor, change_cursor)
        if job.config_key != config.key:
            raise ConflictError("semantic configuration changed")
        if (
            job.request_id is not None
            or job.attempts >= 3
            or not maximum.is_finite()
            or not 0 < maximum <= config.request_usd
            or await self.spent(principal, job.day) + maximum > config.daily_usd
        ):
            return None
        value = SemanticSpend(
            id=self._ids.new_id(),
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            config_key=config.key,
            lease_token=token,
            day=job.day,
            request_digest=digest,
            maximum_usd=maximum,
        )
        await self._session.execute(
            pg_insert(SemanticSpendRow).values(
                id=value.id,
                tenant_id=value.tenant_id,
                principal_id=value.principal_id,
                day=value.day,
                charge=value.charge,
                payload=value.model_dump(mode="json"),
            )
        )
        await self._save(
            job.model_copy(
                update={
                    "request_id": value.id,
                    "next_full": full_cursor,
                    "next_change": change_cursor,
                    "attempts": job.attempts + 1,
                }
            )
        )
        await self._session.flush()
        return value

    async def settle(
        self,
        principal: Principal,
        token: UUID,
        spend_id: UUID,
        actual: Decimal | None,
        now: datetime,
    ) -> SemanticSpend:
        job = await self.current(principal, token, now)
        row = await self._spend(principal, spend_id)
        if row is None or job.request_id != spend_id:
            raise ConflictError("semantic reservation unavailable")
        value = SemanticSpend.model_validate(row.payload)
        if value.lease_token != token:
            raise ConflictError("semantic reservation unavailable")
        result = settled_spend(value, actual)
        row.payload, row.charge = result.model_dump(mode="json"), result.charge
        await self._session.flush()
        return result

    async def finish(
        self,
        principal: Principal,
        token: UUID,
        full_cursor: int,
        change_cursor: int,
        success: bool,
        now: datetime,
    ) -> SemanticJob:
        value = finish_job(
            await self.current(principal, token, now), full_cursor, change_cursor, success
        )
        await self._save(value)
        return value

    async def spent(self, principal: Principal, day: date) -> Decimal:
        await lock_owner(self._session, principal)
        value = await self._session.scalar(
            select(func.sum(SemanticSpendRow.charge)).where(
                SemanticSpendRow.tenant_id == principal.tenant_id,
                SemanticSpendRow.principal_id == principal.principal_id,
                SemanticSpendRow.day == day,
            )
        )
        return Decimal(value or 0)
