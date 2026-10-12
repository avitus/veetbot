"""Transactional in-memory semantic maintenance repository."""

from collections.abc import MutableMapping
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

from agent_core.adapters.memory.transactions import MemoryTransaction
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


class InMemorySemanticWorkStore:
    def __init__(self, transaction: MemoryTransaction, ids: IdFactory) -> None:
        self._transaction = transaction
        self._ids = ids
        self._jobs: MutableMapping[tuple[str, str], SemanticJob] = transaction.mapping()
        self._spend: MutableMapping[tuple[str, str, UUID], SemanticSpend] = transaction.mapping()

    async def claim(
        self, principal: Principal, config: EmbeddingConfig, bounds: SemanticBounds, now: datetime
    ) -> SemanticJob | None:
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
            old = self._jobs.get(owner)
            value = claim_job(
                old,
                tenant=owner[0],
                owner=owner[1],
                config_key=config.key,
                full_bound=bounds.creation,
                change_bound=bounds.change,
                token=self._ids.new_id(),
                now=now,
            )
            if value is not None:
                if old is not None and old.request_id is not None:
                    key = (*owner, old.request_id)
                    spend = self._spend.get(key)
                    if spend is not None and spend.state == "reserved":
                        self._spend[key] = settled_spend(spend, None)
                self._jobs[owner] = value
            return value

    async def current(self, principal: Principal, token: UUID, now: datetime) -> SemanticJob:
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
            return require_job(self._jobs.get(owner), token, now)

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
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
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
                tenant_id=owner[0],
                principal_id=owner[1],
                config_key=config.key,
                lease_token=token,
                day=job.day,
                request_digest=digest,
                maximum_usd=maximum,
            )
            self._spend[*owner, value.id] = value
            self._jobs[owner] = job.model_copy(
                update={
                    "request_id": value.id,
                    "next_full": full_cursor,
                    "next_change": change_cursor,
                    "attempts": job.attempts + 1,
                }
            )
            return value

    async def settle(
        self,
        principal: Principal,
        token: UUID,
        spend_id: UUID,
        actual: Decimal | None,
        now: datetime,
    ) -> SemanticSpend:
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
            job = await self.current(principal, token, now)
            value = self._spend.get((*owner, spend_id))
            if value is None or value.lease_token != token or job.request_id != spend_id:
                raise ConflictError("semantic reservation unavailable")
            result = settled_spend(value, actual)
            self._spend[*owner, spend_id] = result
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
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
            job = await self.current(principal, token, now)
            value = finish_job(job, full_cursor, change_cursor, success)
            self._jobs[owner] = value
            return value

    async def spent(self, principal: Principal, day: date) -> Decimal:
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
            return sum(
                (v.charge for key, v in self._spend.items() if key[:2] == owner and v.day == day),
                Decimal(0),
            )
