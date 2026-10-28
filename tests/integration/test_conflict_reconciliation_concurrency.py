"""Conflict repair must not overwrite a concurrent owner's review (ADR-0248)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.adapters.persistence.people import PostgresPeopleStore
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.memory import (
    BeliefType,
    ConflictReconciliation,
    MemoryRecord,
    MemoryReviewOutcome,
    MemoryStatus,
    Portability,
)
from agent_core.memory.formation import GovernedMemoryService
from agent_core.ports.persistence import RepositoryUnitOfWork
from tests.contract.memory_fixtures import memory, user_event
from tests.contract.support import NOW, principal, session
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("outcome", [MemoryReviewOutcome.NOT_HERE, MemoryReviewOutcome.UNTRUE])
async def test_reconciliation_preserves_review_committed_while_it_starts(
    outcome: MemoryReviewOutcome, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer owns the lock, but repair can still read its old committed row.

    Release the review transaction after repair either reads that stale row (the
    bug) or attempts the owner guard (the fix). No timing sleeps select a winner.
    """
    async with build(
        settings=database_settings(),
        storage="postgres",
        principal=principal(),
        clock=FixedClock(NOW),
    ) as app:
        factory = app.uow_factory
        async with factory() as uow:
            await uow.sessions.create(session())
        await user_event(
            factory, "Andy Vitus lives at 12 Oak Lane, Los Altos Hills and plays trumpet."
        )
        address = memory(
            belief_id=501, statement="Andy Vitus's home address is 12 Oak Lane, Los Altos Hills."
        )
        instrument = memory(belief_id=503, statement="The owner plays trumpet.")
        async with factory() as uow:
            for record, partner in ((address, instrument), (instrument, address)):
                await uow.memories.upsert_belief(
                    record.model_copy(
                        update={
                            "belief_type": BeliefType.FACT,
                            "conflicts_with": [partner.id],
                            "flagged_for_review": True,
                            "store_position": await uow.memories.next_position(),
                        },
                        deep=True,
                    )
                )
        service = GovernedMemoryService(factory, FixedClock(NOW), RandomIdFactory(), principal())
        attempted = asyncio.Event()
        repair_task: asyncio.Task[ConflictReconciliation] | None = None
        original_lock = PostgresPeopleStore.lock
        original_read = GovernedMemoryService._live_conflicted

        @asynccontextmanager
        async def observed_lock(
            store: PostgresPeopleStore, owner: Principal
        ) -> AsyncIterator[None]:
            if asyncio.current_task() is repair_task:
                attempted.set()
            async with original_lock(store, owner):
                yield

        async def observed_read(
            target: GovernedMemoryService, uow: RepositoryUnitOfWork
        ) -> list[MemoryRecord]:
            records = await original_read(target, uow)
            if asyncio.current_task() is repair_task:
                attempted.set()
            return records

        monkeypatch.setattr(PostgresPeopleStore, "lock", observed_lock)
        monkeypatch.setattr(GovernedMemoryService, "_live_conflicted", observed_read)
        # Contexts exit in reverse order: review commits before waiting for repair.
        async with (
            asyncio.timeout(10),
            asyncio.TaskGroup() as tasks,
            factory() as uow,
            uow.people.lock(principal()),
        ):
            reviewed = await service.review(address.id, outcome, existing_uow=uow)
            repair_task = tasks.create_task(service.reconcile_conflicts(confirm=True))
            await attempted.wait()
        async with factory() as uow:
            actual = await uow.memories.get(address.id, principal())
            rejections = await uow.memories.outstanding_rejections(
                principal().tenant_id, principal().principal_id
            )
        assert actual.status == reviewed.status
        assert actual.portability == reviewed.portability
        assert actual.confidence == reviewed.confidence
        assert actual.valid_to == reviewed.valid_to
        assert actual.flagged_for_review
        assert any(rejection.belief_id == address.id for rejection in rejections)
        if outcome is MemoryReviewOutcome.NOT_HERE:
            assert actual.portability is Portability.LOCAL
            assert actual.conflicts_with == []
        else:
            assert actual.status is MemoryStatus.RETIRED
