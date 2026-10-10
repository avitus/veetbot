"""Bounded inventory admission; provider and derived-memory work are separate."""

import asyncio
from collections.abc import Callable
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.reconsolidation import ReconsolidationJob, SourceVersion, group_digest
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory


class ReconsolidationInventoryPass:
    """Queue a bounded inventory slice after explicit admission.

    This foundation is not composed in the production maintenance role until
    provider processing and matching release evidence are available.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        principal: Principal,
        *,
        worker_id: str,
        admitted: Callable[[], bool],
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._principal = principal
        self._worker_id = worker_id
        self._admitted = admitted

    async def _renew(self, token: UUID) -> None:
        while True:
            await asyncio.sleep(30)
            if not self._admitted():
                raise ConflictError("reconsolidation admission withdrawn")
            async with self._uow_factory() as uow:
                await uow.reconsolidation.renew(self._principal, token, self._clock.now())

    async def _inventory(self, lease: ReconsolidationJob) -> int:
        async with self._uow_factory() as uow:
            page = await uow.reconsolidation.inventory(
                self._principal, lease.lease_token, self._clock.now()
            )
        groups: list[tuple[SourceVersion, ...]] = []
        seen: set[str] = set()
        # Rotate equal-priority anchors on subsequent generations. Inventory
        # still progresses in immutable creation order, independent of selection.
        offset = (lease.generation - 1) % max(1, len(page.sources))
        anchors = page.sources[offset:] + page.sources[:offset]
        for source in anchors:
            if not self._admitted():
                return 0
            async with self._uow_factory() as uow:
                sources = await uow.reconsolidation.neighbors(
                    self._principal, source, self._clock.now()
                )
            digest = group_digest(sources)
            if len(sources) > 1 and digest not in seen:
                seen.add(digest)
                groups.append(sources)
            if len(groups) == 4:
                break
        if not self._admitted():
            return 0
        try:
            async with self._uow_factory() as uow:
                queued = await uow.reconsolidation.checkpoint(
                    self._principal, lease.lease_token, page, tuple(groups), self._clock.now()
                )
        except ConflictError:
            # A full queue or changed source leaves this cursor unadvanced.
            # No provider attempt has been made and no retry is consumed.
            return 0
        return len(queued)

    async def run_once(self) -> int:
        if not self._admitted():
            return 0
        async with self._uow_factory() as uow:
            lease = await uow.reconsolidation.claim_due(
                self._principal, self._clock.now(), self._worker_id
            )
        if lease is None:
            return 0
        async with asyncio.timeout(120), asyncio.TaskGroup() as tasks:
            heartbeat = tasks.create_task(self._renew(lease.lease_token))
            try:
                queued = await self._inventory(lease)
                heartbeat.cancel()
                await self._process(lease)
            finally:
                heartbeat.cancel()
        # Cancellation or lease loss intentionally leaves the token fenced until
        # recovery; a successful bounded pass yields immediately to other work.
        async with self._uow_factory() as uow:
            await uow.reconsolidation.release(self._principal, lease.lease_token, self._clock.now())
        return queued

    async def _process(self, lease: ReconsolidationJob) -> int:
        """Inventory-only callers do not claim model work."""
        return 0
