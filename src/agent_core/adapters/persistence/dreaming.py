"""Daily review claims and content-free history serialized by the memory owner guard."""

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.reconsolidation_sources import lock_owner
from agent_core.adapters.persistence.sqlalchemy_models import (
    DreamingRunRow,
    ReconsolidationOwnerRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import DreamingRun, DreamingSchedule
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.determinism import IdFactory


class PostgresDreamingSchedule:
    def __init__(self, session: AsyncSession, ids: IdFactory) -> None:
        self._session, self._ids = session, ids

    async def status(self, principal: Principal) -> DreamingSchedule:
        owner = await lock_owner(self._session, principal)
        return DreamingSchedule.model_validate(owner.dreaming_state)

    async def _save(self, principal: Principal, state: DreamingSchedule) -> None:
        row = ReconsolidationOwnerRow
        await self._session.execute(
            update(row)
            .where(
                row.tenant_id == principal.tenant_id,
                row.principal_id == principal.principal_id,
            )
            .values(dreaming_state=state.model_dump(mode="json"))
        )

    async def pause(self, principal: Principal, paused: bool, revision: int) -> DreamingSchedule:
        state = await self.status(principal)
        if state.paused == paused and state.revision == revision + 1:
            return state
        if state.revision != revision:
            raise ConflictError("dreaming schedule changed")
        state = state.model_copy(update={"paused": paused, "revision": state.revision + 1})
        await self._save(principal, state)
        return state

    async def claim(self, principal: Principal, now: datetime) -> DreamingRun | None:
        state = await self.status(principal)
        if state.paused or (state.next_run_at is not None and now < state.next_run_at):
            return None
        run = DreamingRun(id=self._ids.new_id(), started_at=now)
        await self._save(
            principal,
            state.model_copy(
                update={
                    "next_run_at": now + timedelta(days=1),
                    "revision": state.revision + 1,
                }
            ),
        )
        await self._session.execute(
            pg_insert(DreamingRunRow).values(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                id=run.id,
                started_at=now,
                payload=run.model_dump(mode="json"),
            )
        )
        return run

    async def finish(self, principal: Principal, run: DreamingRun) -> None:
        await lock_owner(self._session, principal)
        row = DreamingRunRow
        predicate = (
            row.tenant_id == principal.tenant_id,
            row.principal_id == principal.principal_id,
            row.id == run.id,
        )
        payload = await self._session.scalar(select(row.payload).where(*predicate))
        if payload is None:
            raise NotFoundError("dreaming run not found")
        prior = DreamingRun.model_validate(payload)
        if prior == run:
            return
        if (
            prior.outcome != "running"
            or prior.started_at != run.started_at
            or run.finished_at is None
        ):
            raise ConflictError("dreaming run already finished or changed")
        await self._session.execute(
            update(row).where(*predicate).values(payload=run.model_dump(mode="json"))
        )

    async def runs(self, principal: Principal, now: datetime) -> tuple[DreamingRun, ...]:
        await lock_owner(self._session, principal)
        row = DreamingRunRow
        payloads = await self._session.scalars(
            select(row.payload)
            .where(
                row.tenant_id == principal.tenant_id,
                row.principal_id == principal.principal_id,
            )
            .order_by(row.started_at.desc(), row.id.desc())
            .limit(50)
        )
        results = tuple(DreamingRun.model_validate(p) for p in payloads)
        return tuple(
            r.model_copy(update={"outcome": "interrupted", "reason": "unreported"})
            if r.outcome == "running" and now >= r.started_at + timedelta(minutes=3)
            else r
            for r in results
        )
