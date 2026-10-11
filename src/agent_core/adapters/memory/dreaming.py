"""Content-free daily review scheduling in the memory transaction journal."""

from collections.abc import MutableMapping
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.adapters.memory.transactions import MemoryTransaction
from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import DreamingRun, DreamingSchedule
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.determinism import IdFactory


class InMemoryDreamingSchedule:
    def __init__(self, transaction: MemoryTransaction, ids: IdFactory) -> None:
        self._transaction, self._ids = transaction, ids
        self._states: MutableMapping[tuple[str, str], DreamingSchedule] = transaction.mapping()
        self._runs: MutableMapping[tuple[str, str, UUID], DreamingRun] = transaction.mapping()

    async def status(self, principal: Principal) -> DreamingSchedule:
        async with self._transaction.owner((principal.tenant_id, principal.principal_id)):
            return self._states.get(
                (principal.tenant_id, principal.principal_id), DreamingSchedule()
            )

    async def pause(self, principal: Principal, paused: bool, revision: int) -> DreamingSchedule:
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
            state = await self.status(principal)
            if state.paused == paused and state.revision == revision + 1:
                return state
            if state.revision != revision:
                raise ConflictError("dreaming schedule changed")
            state = state.model_copy(update={"paused": paused, "revision": state.revision + 1})
            self._states[owner] = state
            return state

    async def claim(self, principal: Principal, now: datetime) -> DreamingRun | None:
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
            state = await self.status(principal)
            if state.paused or (state.next_run_at is not None and now < state.next_run_at):
                return None
            run = DreamingRun(id=self._ids.new_id(), started_at=now)
            self._states[owner] = state.model_copy(
                update={
                    "next_run_at": now + timedelta(days=1),
                    "revision": state.revision + 1,
                }
            )
            self._runs[*owner, run.id] = run
            return run

    async def finish(self, principal: Principal, run: DreamingRun) -> None:
        key = (principal.tenant_id, principal.principal_id, run.id)
        async with self._transaction.owner(key[:2]):
            prior = self._runs.get(key)
            if prior is None:
                raise NotFoundError("dreaming run not found")
            if prior == run:
                return
            if (
                prior.outcome != "running"
                or prior.started_at != run.started_at
                or run.finished_at is None
            ):
                raise ConflictError("dreaming run already finished or changed")
            self._runs[key] = run

    async def runs(self, principal: Principal, now: datetime) -> tuple[DreamingRun, ...]:
        owner = (principal.tenant_id, principal.principal_id)
        async with self._transaction.owner(owner):
            results = sorted(
                (r for key, r in self._runs.items() if key[:2] == owner),
                key=lambda r: (r.started_at, r.id),
                reverse=True,
            )[:50]
            return tuple(
                r.model_copy(update={"outcome": "interrupted", "reason": "unreported"})
                if r.outcome == "running" and now >= r.started_at + timedelta(minutes=3)
                else r
                for r in results
            )
