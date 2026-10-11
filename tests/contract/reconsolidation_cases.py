"""Identical observable maintenance scenarios for both storage adapters."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import MemoryDerivation, Portability, Sensitivity
from agent_core.ports.events import EventRepository
from agent_core.ports.memory import MemoryStore
from agent_core.ports.people import PeopleStore
from agent_core.ports.reconsolidation import ReconsolidationStore
from tests.contract.memory_fixtures import memory
from tests.contract.support import NOW, principal


@dataclass
class Stores:
    memories: MemoryStore
    reconsolidation: ReconsolidationStore
    events: EventRepository
    people: PeopleStore


type Factory = Callable[[], AbstractAsyncContextManager[Stores]]


async def leases(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        first = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert first is not None
    async with factory() as uow:
        assert await uow.reconsolidation.claim_due(principal(), NOW, "b") is None
        renewed = await uow.reconsolidation.renew(
            principal(), first.lease_token, NOW + timedelta(seconds=30)
        )
        assert renewed.lease_token == first.lease_token
    later = NOW + timedelta(seconds=211)
    async with factory() as uow:
        recovered = await uow.reconsolidation.claim_due(principal(), later, "b")
        assert recovered is not None and recovered.id == first.id
        assert recovered.lease_expirations == 1
        assert recovered.lease_token != first.lease_token
    async with factory() as uow:
        with pytest.raises(ConflictError):
            await uow.reconsolidation.renew(principal(), first.lease_token, later)
    for _ in range(5):
        async with factory() as uow:
            await uow.reconsolidation.release(principal(), recovered.lease_token, later)
            recovered = await uow.reconsolidation.claim_due(principal(), later, "c")
            assert recovered is not None and recovered.lease_expirations == 1


async def source_revisions(factory: Factory) -> None:
    async with factory() as uow:
        original = memory()
        await uow.memories.upsert_belief(original)
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        before = page.sources[0]
        used = original.model_copy(
            update={"utility": 0.4, "last_used_at": NOW, "updated_at": NOW + timedelta(seconds=1)}
        )
        await uow.memories.reinforce(used)
        same = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        assert same.sources[0] == before
        await uow.memories.reinforce(used.model_copy(update={"confidence": 0.8}))
        changed = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        assert changed.sources[0].creation_sequence == before.creation_sequence
        assert changed.sources[0].content_revision == before.content_revision + 1
        assert (
            await uow.memories.get(original.id, principal())
        ).last_evidence_at == original.last_evidence_at


async def inventory_fairness(factory: Factory) -> None:
    async with factory() as uow:
        for i in range(160):
            await uow.memories.upsert_belief(
                memory(belief_id=1000 + i).model_copy(update={"store_position": i + 1})
            )
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None and lease.full_bound == 160
        # A backdated insert and changes to recent rows cannot move the bound.
        await uow.memories.upsert_belief(
            memory(belief_id=3000).model_copy(
                update={"store_position": 1000, "created_at": NOW - timedelta(days=100)}
            )
        )
    visited: set[UUID] = set()
    while True:
        async with factory() as uow:
            page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
            assert page.inspected <= 128
            assert page.full_cursor - lease.full_cursor >= min(
                64, lease.full_bound - lease.full_cursor
            )
            visited.update(s.belief_id for s in page.sources)
            await uow.reconsolidation.checkpoint(principal(), lease.lease_token, page, (), NOW)
            released = await uow.reconsolidation.release(principal(), lease.lease_token, NOW)
            if released.state == "complete":
                break
            lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
            assert lease is not None
    assert visited == {UUID(int=1000 + i) for i in range(160)}


async def scope_and_erasure(factory: Factory) -> None:
    async with factory() as uow:
        for i, updates in enumerate(
            (
                {},
                {"principal_id": "foreign"},
                {"sensitivity": Sensitivity.RESTRICTED},
                {"derivation": MemoryDerivation.HYPOTHESIS},
            )
        ):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + i).model_copy(
                    update={"store_position": i + 1, **cast(dict[str, Any], updates)}
                )
            )
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        assert {s.belief_id for s in page.sources} == {UUID(int=501)}
        await uow.memories.fence_for_erasure(principal(), [UUID(int=501)])
        assert not (
            await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        ).sources
        foreign = principal().model_copy(update={"principal_id": "foreign"})
        with pytest.raises(NotFoundError):
            await uow.reconsolidation.inventory(foreign, lease.lease_token, NOW)


async def groups_and_recovery(factory: Factory) -> None:
    async with factory() as uow:
        for i in range(40):
            await uow.memories.upsert_belief(
                memory(belief_id=600 + i).model_copy(update={"store_position": i + 1})
            )
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        neighbors = await uow.reconsolidation.neighbors(principal(), page.sources[0], NOW)
        assert len(neighbors) == 32
        queued = await uow.reconsolidation.checkpoint(
            principal(), lease.lease_token, page, (neighbors, tuple(reversed(neighbors))), NOW
        )
        assert len(queued) == 1
    now = NOW
    for attempt in range(1, 4):
        async with factory() as uow:
            group = await uow.reconsolidation.claim_group(principal(), lease.lease_token, now)
            assert group is not None and group.attempts == attempt
        now += timedelta(seconds=181)
        async with factory() as uow:
            lease = await uow.reconsolidation.claim_due(principal(), now, "recovery")
            assert lease is not None
    async with factory() as uow:
        assert await uow.reconsolidation.claim_group(principal(), lease.lease_token, now) is None


async def spend(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())  # keep the scan unfinished across slices
    for i in range(8):
        async with factory() as uow:
            lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
            assert lease is not None
            reservation = await uow.reconsolidation.reserve(
                principal(), lease.lease_token, f"{i:064x}", Decimal("0.25"), NOW
            )
            receipt = await uow.reconsolidation.settle(
                principal(), lease.lease_token, reservation.id, None, NOW
            )
            assert receipt.charged_usd == Decimal("0.25") and receipt.state == "unknown"
            await uow.reconsolidation.release(principal(), lease.lease_token, NOW)
    async with factory() as uow:
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        with pytest.raises(ConflictError, match="budget"):
            await uow.reconsolidation.reserve(
                principal(), lease.lease_token, "f" * 64, Decimal("0.01"), NOW
            )


async def midnight_and_settlement(factory: Factory) -> None:
    now = NOW.replace(hour=23, minute=59, second=40)
    async with factory() as uow:
        lease = await uow.reconsolidation.claim_due(principal(), now, "a")
        assert lease is not None
        first = await uow.reconsolidation.reserve(
            principal(), lease.lease_token, "a" * 64, Decimal("0.20"), now
        )
        await uow.reconsolidation.settle(
            principal(), lease.lease_token, first.id, Decimal("0.05"), now
        )
        second = await uow.reconsolidation.reserve(
            principal(), lease.lease_token, "b" * 64, Decimal("0.20"), now + timedelta(seconds=30)
        )
        assert second.day == now.date()
        with pytest.raises(ConflictError, match="budget"):
            await uow.reconsolidation.reserve(
                principal(),
                lease.lease_token,
                "c" * 64,
                Decimal("0.01"),
                now + timedelta(seconds=30),
            )
    async with factory() as uow:
        recovered = await uow.reconsolidation.claim_due(
            principal(), now + timedelta(seconds=181), "b"
        )
        assert recovered is not None
        with pytest.raises(ConflictError):
            await uow.reconsolidation.settle(
                principal(), lease.lease_token, second.id, Decimal(0), now + timedelta(seconds=181)
            )


async def grouping_boundaries(factory: Factory) -> None:
    async with factory() as uow:
        anchor = memory().model_copy(update={"scope": "user", "portability": Portability.LOCAL})
        await uow.memories.upsert_belief(anchor)
        await uow.memories.upsert_belief(
            memory(belief_id=502).model_copy(update={"scope": "user", "store_position": 2})
        )
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        source = next(s for s in page.sources if s.belief_id == anchor.id)
        assert await uow.reconsolidation.neighbors(principal(), source, NOW) == (source,)


async def pending_groups_continue_after_inventory(factory: Factory) -> None:
    async with factory() as uow:
        for i in range(2):
            await uow.memories.upsert_belief(
                memory(belief_id=600 + i).model_copy(update={"store_position": i + 1})
            )
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        await uow.reconsolidation.checkpoint(
            principal(), lease.lease_token, page, (page.sources,), NOW
        )
        released = await uow.reconsolidation.release(principal(), lease.lease_token, NOW)
        assert released.state == "ready", "inventory completion must not strand pending analysis"
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "b")
        assert lease is not None and lease.full_cursor == lease.full_bound
        group = await uow.reconsolidation.claim_group(principal(), lease.lease_token, NOW)
        assert group is not None
        await uow.reconsolidation.finish_group(
            principal(), lease.lease_token, group.id, "no_change", NOW
        )
        assert (
            await uow.reconsolidation.release(principal(), lease.lease_token, NOW)
        ).state == "complete"
        assert await uow.reconsolidation.claim_due(principal(), NOW, "a") is None


async def exact_spend_precision(factory: Factory) -> None:
    async with factory() as uow:
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        with pytest.raises(ValueError):
            await uow.reconsolidation.reserve(
                principal(), lease.lease_token, "a" * 64, Decimal("0.00000000001"), NOW
            )
        reservation = await uow.reconsolidation.reserve(
            principal(), lease.lease_token, "b" * 64, Decimal("0.1"), NOW
        )
        with pytest.raises(ValueError):
            await uow.reconsolidation.settle(
                principal(), lease.lease_token, reservation.id, Decimal("0.00000000001"), NOW
            )
        receipt = await uow.reconsolidation.settle(
            principal(), lease.lease_token, reservation.id, Decimal("0.0123456789"), NOW
        )
        assert receipt.charged_usd == Decimal("0.0123456789")


async def transaction_rollback(factory: Factory) -> None:
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.memories.upsert_belief(memory())
            lease = await uow.reconsolidation.claim_due(principal(), NOW, "aborted")
            assert lease is not None and lease.full_bound == lease.change_bound == 1
            raise RuntimeError("abort")
    async with factory() as uow:
        with pytest.raises(NotFoundError):
            await uow.memories.get(memory().id, principal())
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "committed")
        assert lease is not None and lease.full_bound == lease.change_bound == 0


async def checkpoint_and_spend_rollback(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        await uow.memories.upsert_belief(
            memory(belief_id=502).model_copy(update={"store_position": 2})
        )
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.reconsolidation.checkpoint(
                principal(), lease.lease_token, page, (page.sources,), NOW
            )
            await uow.reconsolidation.reserve(
                principal(), lease.lease_token, "a" * 64, Decimal("0.25"), NOW
            )
            await uow.memories.fence_for_erasure(principal(), [memory().id])
            await uow.memories.purge_erased(principal(), [memory().id], operation_id=UUID(int=800))
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await uow.memories.get(memory().id, principal()) == memory()
        assert (
            await uow.memories.outstanding_rejections(
                principal().tenant_id, principal().principal_id
            )
            == []
        )
        assert await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW) == page
        # The aborted checkpoint, group, request digest and budget must all be reusable.
        groups = await uow.reconsolidation.checkpoint(
            principal(), lease.lease_token, page, (page.sources,), NOW
        )
        assert len(groups) == 1
        await uow.reconsolidation.reserve(
            principal(), lease.lease_token, "a" * 64, Decimal("0.25"), NOW
        )


async def cancelled_transaction_releases_fence(factory: Factory) -> None:
    inserted = asyncio.Event()

    async def writer() -> None:
        async with factory() as uow:
            await uow.memories.upsert_belief(memory())
            await uow.reconsolidation.claim_due(principal(), NOW, "cancelled")
            inserted.set()
            await asyncio.Event().wait()

    async with asyncio.timeout(10):
        task = asyncio.create_task(writer())
        try:
            await inserted.wait()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.memories.get(memory().id, principal())
            lease = await uow.reconsolidation.claim_due(principal(), NOW, "after-cancel")
            assert lease is not None and lease.full_bound == lease.change_bound == 0


async def inflight_source_isolation(factory: Factory) -> None:
    inserted, finish = asyncio.Event(), asyncio.Event()

    async def writer() -> None:
        with pytest.raises(RuntimeError, match="abort"):
            async with factory() as uow:
                await uow.memories.upsert_belief(memory())
                inserted.set()
                await finish.wait()
                raise RuntimeError("abort")

    async def scanner() -> None:
        async with factory() as uow:
            lease = await uow.reconsolidation.claim_due(principal(), NOW, "scanner")
            assert lease is not None and lease.full_bound == lease.change_bound == 0
            await uow.memories.upsert_belief(memory(belief_id=502))

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(writer())
        await inserted.wait()
        reader = tasks.create_task(scanner())
        await asyncio.sleep(0.05)
        assert not reader.done(), "a child task must not inherit the writer's lock ownership"
        finish.set()
    async with factory() as uow:
        assert await uow.memories.get(memory(belief_id=502).id, principal()) == memory(
            belief_id=502
        )


async def changed_inventory_precedes_full_lane(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        other = memory(belief_id=502).model_copy(update={"store_position": 2})
        await uow.memories.upsert_belief(other)
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        await uow.reconsolidation.checkpoint(principal(), lease.lease_token, page, (), NOW)
        assert (
            await uow.reconsolidation.release(principal(), lease.lease_token, NOW)
        ).state == "complete"
        await uow.memories.reinforce(other.model_copy(update={"confidence": 0.8}))
    tomorrow = NOW + timedelta(days=1)
    async with factory() as uow:
        lease = await uow.reconsolidation.claim_due(principal(), tomorrow, "b")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, tomorrow)
        assert [source.belief_id for source in page.sources] == [other.id, memory().id]


async def checkpoint_cannot_skip_inventory(factory: Factory) -> None:
    async with factory() as uow:
        for i in range(160):
            await uow.memories.upsert_belief(
                memory(belief_id=1000 + i).model_copy(update={"store_position": i + 1})
            )
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
        for updates in (
            {"full_cursor": 128, "change_cursor": 0},
            {"sources": ()},
            {"excluded": 1},
        ):
            with pytest.raises(ConflictError):
                await uow.reconsolidation.checkpoint(
                    principal(), lease.lease_token, page.model_copy(update=updates), (), NOW
                )
        assert await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW) == page
        await uow.reconsolidation.checkpoint(principal(), lease.lease_token, page, (), NOW)


async def source_history_and_revision_rollback(factory: Factory) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory())
        lease = await uow.reconsolidation.claim_due(principal(), NOW, "a")
        assert lease is not None
        before = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.memories.reinforce(
                original.model_copy(update={"statement": "Changed claim", "store_position": 5})
            )
            await uow.memories.set_consolidation_watermark(
                original.source_session_id, principal(), 20
            )
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await uow.memories.get(original.id, principal()) == original
        assert await uow.memories.get_at(original.id, principal(), known_at=NOW) == original
        assert (
            await uow.memories.consolidation_watermark(original.source_session_id, principal()) == 0
        )
        assert await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW) == before


async def owner_rollback_preserves_another_owners_checkpoint(factory: Factory) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    other = principal().model_copy(update={"principal_id": "other-owner"})
    other_belief = memory(belief_id=502).model_copy(
        update={"principal_id": other.principal_id, "store_position": 2}
    )

    async def aborted_owner() -> None:
        with pytest.raises(RuntimeError, match="abort"):
            async with factory() as uow:
                await uow.memories.upsert_belief(memory())
                lease = await uow.reconsolidation.claim_due(principal(), NOW, "aborted")
                assert lease is not None
                page = await uow.reconsolidation.inventory(principal(), lease.lease_token, NOW)
                await uow.reconsolidation.checkpoint(principal(), lease.lease_token, page, (), NOW)
                entered.set()
                await finish.wait()
                raise RuntimeError("abort")

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(aborted_owner())
        await entered.wait()
        async with factory() as uow:
            await uow.memories.upsert_belief(other_belief)
            lease = await uow.reconsolidation.claim_due(other, NOW, "independent")
            assert lease is not None
            page = await uow.reconsolidation.inventory(other, lease.lease_token, NOW)
            await uow.reconsolidation.checkpoint(other, lease.lease_token, page, (), NOW)
        finish.set()
    async with factory() as uow:
        assert await uow.memories.get(other_belief.id, other) == other_belief
        page = await uow.reconsolidation.inventory(other, lease.lease_token, NOW)
        with pytest.raises(ConflictError, match="already checkpointed"):
            await uow.reconsolidation.checkpoint(other, lease.lease_token, page, (), NOW)


SCENARIOS = (
    owner_rollback_preserves_another_owners_checkpoint,
    source_history_and_revision_rollback,
    changed_inventory_precedes_full_lane,
    checkpoint_cannot_skip_inventory,
    cancelled_transaction_releases_fence,
    inflight_source_isolation,
    transaction_rollback,
    checkpoint_and_spend_rollback,
    leases,
    source_revisions,
    inventory_fairness,
    scope_and_erasure,
    groups_and_recovery,
    spend,
    midnight_and_settlement,
    grouping_boundaries,
    pending_groups_continue_after_inventory,
    exact_spend_precision,
)
