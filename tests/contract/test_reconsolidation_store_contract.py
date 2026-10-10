"""Shared M32 maintenance behavior; also exercised against PostgreSQL."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.adapters.memory.dreaming import InMemoryDreamingSchedule
from agent_core.adapters.memory.in_memory import InMemoryMemoryStore
from agent_core.adapters.memory.reconsolidation import InMemoryReconsolidationStore
from agent_core.adapters.memory.reconsolidation_index import ReconsolidationIndex
from agent_core.adapters.memory.transactions import MemoryTransaction
from tests.contract.reconsolidation_admission_cases import ADMISSION_SCENARIOS
from tests.contract.reconsolidation_apply_cases import APPLY_SCENARIOS
from tests.contract.reconsolidation_attribution_cases import ATTRIBUTION_SCENARIOS
from tests.contract.reconsolidation_audit_cases import AUDIT_SCENARIOS
from tests.contract.reconsolidation_cases import SCENARIOS, Factory, Stores
from tests.contract.reconsolidation_execution_cases import EXECUTION_SCENARIOS
from tests.contract.reconsolidation_input_cases import INPUT_SCENARIOS
from tests.contract.reconsolidation_operation_cases import OPERATION_SCENARIOS
from tests.contract.reconsolidation_source_cases import MERGE_SCENARIOS
from tests.contract.reconsolidation_summary_cases import SUMMARY_SCENARIOS
from tests.contract.support import NOW, memory_uow_factory, principal


async def test_due_owner_is_claimed_once() -> None:
    memories = InMemoryMemoryStore(FixedClock(NOW), ReconsolidationIndex(MemoryTransaction()))
    store = InMemoryReconsolidationStore(
        memories,
        RandomIdFactory(),
        dreaming=InMemoryDreamingSchedule(memories._transaction, RandomIdFactory()),
    )
    first = await store.claim_due(principal(), NOW, "worker-a")
    assert first is not None, "a due owner needs a durable job lease"
    assert await store.claim_due(principal(), NOW, "worker-b") is None


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case.__name__)
async def test_shared_contract(scenario: Callable[[Factory], Awaitable[None]]) -> None:
    _, uow_factory = await memory_uow_factory()

    @asynccontextmanager
    async def factory() -> AsyncIterator[Stores]:
        async with uow_factory() as uow:
            yield Stores(uow.memories, uow.reconsolidation, uow.events, uow.people)

    await scenario(factory)


async def test_full_scan_covers_100000_originals_despite_recent_churn() -> None:
    from datetime import timedelta
    from math import ceil
    from uuid import UUID

    from tests.contract.memory_fixtures import memory

    memories = InMemoryMemoryStore(FixedClock(NOW), ReconsolidationIndex(MemoryTransaction()))
    store = InMemoryReconsolidationStore(
        memories,
        RandomIdFactory(),
        dreaming=InMemoryDreamingSchedule(memories._transaction, RandomIdFactory()),
    )
    count = 100_000
    for i in range(count):
        await memories.upsert_belief(
            memory(belief_id=1000 + i).model_copy(update={"store_position": i + 1})
        )
    lease = await store.claim_due(principal(), NOW, "scan")
    assert lease is not None and lease.full_bound == count
    visited: set[UUID] = set()
    for step in range(ceil(count / 64)):
        async with memories._transaction.transaction():
            recent = await memories.get(UUID(int=1000 + count - 1), principal())
            await memories.reinforce(
                recent.model_copy(update={"confidence": 0.8 if step % 2 else 0.9})
            )
            await memories.upsert_belief(
                memory(belief_id=200_000 + step).model_copy(
                    update={
                        "store_position": count + step + 1,
                        "created_at": NOW - timedelta(days=100),
                    }
                )
            )
            page = await store.inventory(principal(), lease.lease_token, NOW)
            assert page.inspected <= 128
            assert page.full_cursor - lease.full_cursor >= min(64, count - lease.full_cursor)
            visited.update(s.belief_id for s in page.sources)
            await store.checkpoint(principal(), lease.lease_token, page, (), NOW)
            result = await store.release(principal(), lease.lease_token, NOW)
            if result.state == "complete":
                break
            lease = await store.claim_due(principal(), NOW, "scan")
            assert lease is not None
    assert result.state == "complete"
    assert visited == {UUID(int=1000 + i) for i in range(count)}


async def test_people_and_memory_use_the_same_lock_order() -> None:
    import asyncio

    from tests.contract.memory_fixtures import memory

    _, factory = await memory_uow_factory()
    entered, proceed = asyncio.Event(), asyncio.Event()
    # These are the actual composed repositories, also usable without a UOW.
    repositories = factory._repositories

    async def writer() -> None:
        async with factory() as uow:
            await uow.memories.upsert_belief(memory())
            entered.set()
            await proceed.wait()
            async with uow.people.lock(principal()):
                assert await uow.memories.get(memory().id, principal()) == memory()

    async def direct_reader() -> None:
        async with repositories.people.lock(principal()):
            assert await repositories.memories.get(memory().id, principal()) == memory()

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        tasks.create_task(writer())
        await entered.wait()
        direct = tasks.create_task(direct_reader())
        await asyncio.sleep(0.05)
        assert not direct.done()
        proceed.set()


async def test_inner_commit_remains_reversible_by_outer_memory_uow() -> None:
    from agent_core.domain.errors import NotFoundError
    from tests.contract.memory_fixtures import memory

    _, factory = await memory_uow_factory()
    with pytest.raises(RuntimeError, match="outer abort"):
        async with factory() as outer:
            await outer.memories.upsert_belief(memory())
            async with factory() as inner:
                await inner.memories.upsert_belief(memory(belief_id=502))
            raise RuntimeError("outer abort")
    async with factory() as uow:
        for key in (memory().id, memory(belief_id=502).id):
            with pytest.raises(NotFoundError):
                await uow.memories.get(key, principal())


async def test_child_task_does_not_inherit_memory_transaction_ownership() -> None:
    import asyncio

    from tests.contract.memory_fixtures import memory

    _, factory = await memory_uow_factory()

    async def child_reader() -> None:
        async with factory() as uow:
            assert await uow.memories.get(memory().id, principal()) == memory()

    async with asyncio.timeout(5):
        async with factory() as uow:
            await uow.memories.upsert_belief(memory())
            child = asyncio.create_task(child_reader())
            try:
                await asyncio.sleep(0.05)
                assert not child.done(), "uncommitted parent state must be isolated from its child"
            except BaseException:
                child.cancel()
                await asyncio.gather(child, return_exceptions=True)
                raise
        await child


async def test_aborted_email_cleanup_preserves_other_owners_memory_history() -> None:
    import asyncio

    from tests.contract.memory_fixtures import memory

    _, factory = await memory_uow_factory()
    other = principal().model_copy(update={"principal_id": "unaffected-owner"})
    original = memory().model_copy(update={"principal_id": other.principal_id})
    revised = original.model_copy(update={"confidence": 0.8})
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
    cleaned, finish = asyncio.Event(), asyncio.Event()

    async def cleanup() -> None:
        with pytest.raises(RuntimeError, match="abort"):
            async with factory() as uow:
                await uow.session_deletions.erase_email_source(
                    principal(), "account", "no-source-thread", frozenset(), NOW
                )
                cleaned.set()
                await finish.wait()
                raise RuntimeError("abort")

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        tasks.create_task(cleanup())
        await cleaned.wait()
        async with factory() as uow:
            await uow.memories.reinforce(revised)
        finish.set()
    async with factory() as uow:
        assert await uow.memories.get(original.id, other) == revised
        assert await uow.memories.get_at(original.id, other, known_at=NOW) == revised


@pytest.mark.parametrize(
    "scenario",
    [
        *MERGE_SCENARIOS,
        *ATTRIBUTION_SCENARIOS,
        *OPERATION_SCENARIOS,
        *SUMMARY_SCENARIOS,
        *INPUT_SCENARIOS,
        *ADMISSION_SCENARIOS,
        *EXECUTION_SCENARIOS,
        *APPLY_SCENARIOS,
        *AUDIT_SCENARIOS,
    ],
    ids=lambda case: case.__name__,
)
async def test_merge_source_contract(scenario: Callable[[Factory], Awaitable[None]]) -> None:
    _, uow_factory = await memory_uow_factory()

    @asynccontextmanager
    async def factory() -> AsyncIterator[Stores]:
        async with uow_factory() as uow:
            yield Stores(uow.memories, uow.reconsolidation, uow.events, uow.people)

    await scenario(factory)


async def test_in_memory_summary_erasure_removes_stored_text_synchronously() -> None:
    from tests.contract.reconsolidation_summary_cases import committed_summary

    _, uow_factory = await memory_uow_factory()

    @asynccontextmanager
    async def factory() -> AsyncIterator[Stores]:
        async with uow_factory() as uow:
            yield Stores(uow.memories, uow.reconsolidation, uow.events, uow.people)

    value = await committed_summary(factory, omitted=True)
    async with factory() as uow:
        assert isinstance(uow.memories, InMemoryMemoryStore)
        index = uow.memories._reconsolidation_index
        assert value.id in index.summaries
        await uow.memories.fence_for_erasure(principal(), list(value.plan.omitted_source_ids))
        assert value.id not in index.summaries
        assert index.operations[value.id].state == "invalidated"
        assert all("prefer" not in op.model_dump_json() for op in index.history.values())
