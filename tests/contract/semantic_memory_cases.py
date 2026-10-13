"""The same semantic lifecycle behavior is required of both persistence adapters."""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.domain.memory import MemoryStatus, Portability, Sensitivity
from agent_core.domain.semantic_memory import EmbeddingSpace
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.support import NOW, principal

SPACE = EmbeddingSpace(provider="fixture", model="semantic", revision="v1", dimensions=3)
VECTOR = (1.0, 0.0, 0.0)


async def indexed_original(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory(statement="User avoids dairy"))
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None, "live originals need a version-bound index source"
        assert await uow.semantic_memory.put(principal(), source, VECTOR)
    async with factory() as uow:
        hits = await uow.semantic_memory.search(
            recall_query(text="breakfast suggestions"), SPACE, VECTOR, limit=1
        )
        assert [hit.record for hit in hits] == [original]
        assert hits[0].similarity == pytest.approx(1)
        assert await uow.memories.get(original.id, principal()) == original
        assert await uow.semantic_memory.put(principal(), source, VECTOR), (
            "safe retry is idempotent"
        )
        other = SPACE.model_copy(update={"revision": "v2"})
        assert await uow.semantic_memory.search(recall_query(), other, VECTOR, limit=1) == []
        assert (
            await uow.semantic_memory.search(
                recall_query(principal_id="foreign"), SPACE, VECTOR, limit=1
            )
            == []
        )
        assert (
            await uow.semantic_memory.search(
                recall_query(tenant_id="foreign"), SPACE, VECTOR, limit=1
            )
            == []
        )
        assert not await uow.semantic_memory.put(
            principal().model_copy(update={"principal_id": "foreign"}), source, VECTOR
        )


async def correction_rejects_old_response(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory())
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None
        assert await uow.semantic_memory.put(principal(), source, VECTOR)
    async with factory() as uow:
        await uow.memories.reinforce(
            original.model_copy(update={"statement": "User prefers detailed explanations"})
        )
    async with factory() as uow:
        assert await uow.semantic_memory.search(recall_query(), SPACE, VECTOR, limit=1) == []
        assert not await uow.semantic_memory.put(principal(), source, VECTOR)
        current = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert (
            current is not None and current.source.content_revision > source.source.content_revision
        )
        assert current.content_digest != source.content_digest
        assert await uow.semantic_memory.put(principal(), current, VECTOR)
        assert (await uow.semantic_memory.search(recall_query(), SPACE, VECTOR, limit=1))[
            0
        ].record.statement == current.record.statement


async def restriction_and_erasure_fence_jobs(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory())
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None
        assert await uow.semantic_memory.put(principal(), source, VECTOR)
    async with factory() as uow:
        await uow.memories.reinforce(
            original.model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
        )
    async with factory() as uow:
        assert not await uow.semantic_memory.put(principal(), source, VECTOR)
        assert await uow.semantic_memory.search(recall_query(), SPACE, VECTOR, limit=1) == []
        restricted = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert restricted is not None
        assert await uow.semantic_memory.put(principal(), restricted, VECTOR)
        assert (
            await uow.semantic_memory.search(
                recall_query(sensitivity_ceiling=Sensitivity.INTERNAL), SPACE, VECTOR, limit=1
            )
            == []
        )
        await uow.memories.fence_for_erasure(principal(), [original.id])
    async with factory() as uow:
        assert not await uow.semantic_memory.put(principal(), restricted, VECTOR)
        assert await uow.semantic_memory.capture(principal(), original.id, SPACE) is None
        assert (
            await uow.semantic_memory.search(
                recall_query(sensitivity_ceiling=Sensitivity.RESTRICTED), SPACE, VECTOR, limit=1
            )
            == []
        )


async def predicates_before_limit(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        good = await uow.memories.upsert_belief(memory(statement="User avoids lactose"))
        source = await uow.semantic_memory.capture(principal(), good.id, SPACE)
        assert source is not None
        assert await uow.semantic_memory.put(principal(), source, (0.8, 0.6, 0.0))
        for i in range(70):
            value = memory(belief_id=600 + i).model_copy(
                update={"store_position": i + 2, "sensitivity": Sensitivity.RESTRICTED}
            )
            await uow.memories.upsert_belief(value)
            source = await uow.semantic_memory.capture(principal(), value.id, SPACE)
            assert source is not None
            assert await uow.semantic_memory.put(principal(), source, VECTOR)
        hits = await uow.semantic_memory.search(
            recall_query(text="breakfast", sensitivity_ceiling=Sensitivity.INTERNAL),
            SPACE,
            VECTOR,
            limit=1,
        )
        assert [h.record.id for h in hits] == [good.id], (
            "ineligible nearest vectors must not starve eligible older evidence"
        )
        assert (
            await uow.semantic_memory.search(
                recall_query(exclude_ids=(good.id,), sensitivity_ceiling=Sensitivity.INTERNAL),
                SPACE,
                VECTOR,
                limit=1,
            )
            == []
        )
        await uow.memories.reinforce(
            good.model_copy(update={"portability": Portability.LOCAL, "scope": "elsewhere"})
        )
        source = await uow.semantic_memory.capture(principal(), good.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
        assert (
            await uow.semantic_memory.search(
                recall_query(subjects=[], sensitivity_ceiling=Sensitivity.INTERNAL),
                SPACE,
                VECTOR,
                limit=1,
            )
            == []
        )


async def usage_is_not_content(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory())
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None
        await uow.memories.reinforce(
            original.model_copy(update={"utility": 0.8, "last_used_at": NOW, "store_position": 999})
        )
        assert await uow.semantic_memory.put(principal(), source, VECTOR)
        hits = await uow.semantic_memory.search(recall_query(), SPACE, VECTOR, limit=1)
        assert hits[0].record.utility == 0.8


async def rollback_restores_index(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory())
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None
        assert await uow.semantic_memory.put(principal(), source, VECTOR)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.memories.reinforce(
                original.model_copy(update={"statement": "Temporary correction"})
            )
            assert await uow.semantic_memory.search(recall_query(), SPACE, VECTOR, limit=1) == []
            raise RuntimeError("abort")
    async with factory() as uow:
        assert len(await uow.semantic_memory.search(recall_query(), SPACE, VECTOR, limit=1)) == 1


async def finite_fair_inventory(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        for i in range(5):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + i).model_copy(update={"store_position": i + 1})
            )
        bounds = await uow.semantic_memory.bounds(principal())
        assert bounds.creation == bounds.change == 5
        await uow.memories.upsert_belief(
            memory(belief_id=999).model_copy(update={"store_position": 99})
        )
        cursor = 0
        seen: list[UUID] = []
        while cursor < bounds.creation:
            page = await uow.semantic_memory.inventory(
                principal(), after=cursor, through=bounds.creation, limit=2
            )
            assert cursor < page.cursor <= bounds.creation and len(page.sources) <= 2
            seen.extend(s.belief_id for s in page.sources)
            cursor = page.cursor
        assert seen == [UUID(int=501 + i) for i in range(5)]
        original = await uow.memories.get(UUID(int=501), principal())
        await uow.memories.reinforce(original.model_copy(update={"confidence": 0.7}))
        latest = await uow.semantic_memory.bounds(principal())
        page = await uow.semantic_memory.changes(
            principal(), after=bounds.change, through=latest.change, limit=2
        )
        assert page.cursor == latest.change
        assert [c.source.belief_id for c in page.changes] == [UUID(int=999), original.id]
        assert (
            await uow.semantic_memory.bounds(
                principal().model_copy(update={"principal_id": "foreign"})
            )
        ).creation == 0


async def expired_and_historical_are_not_current_vectors(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        changes: tuple[dict[str, Any], ...] = (
            {"status": MemoryStatus.RETIRED},
            {"valid_to": NOW},
            {"expires_at": NOW},
            {"valid_from": NOW + timedelta(days=1)},
        )
        for i, change in enumerate(changes):
            value = memory(belief_id=600 + i).model_copy(update={**change, "store_position": i + 2})
            await uow.memories.upsert_belief(value)
            assert await uow.semantic_memory.capture(principal(), value.id, SPACE) is None
        original = await uow.memories.upsert_belief(memory())
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
        assert (
            await uow.semantic_memory.search(recall_query(as_of=NOW), SPACE, VECTOR, limit=1) == []
        )
        assert (
            await uow.semantic_memory.search(
                recall_query().model_copy(update={"known_at": NOW}), SPACE, VECTOR, limit=1
            )
            == []
        )
        for bad in ((0.0, 0.0, 0.0), (float("nan"), 1.0, 0.0), (1.0, 0.0)):
            with pytest.raises(ValueError):
                await uow.semantic_memory.put(principal(), source, bad)


SCENARIOS = [
    indexed_original,
    correction_rejects_old_response,
    restriction_and_erasure_fence_jobs,
    predicates_before_limit,
    usage_is_not_content,
    rollback_restores_index,
    finite_fair_inventory,
    expired_and_historical_are_not_current_vectors,
]


async def pending_put_rolls_back_and_erased_cursor_progresses(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory())
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            assert await uow.semantic_memory.put(principal(), source, VECTOR)
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await uow.semantic_memory.search(recall_query(), SPACE, VECTOR, limit=1) == []
        await uow.memories.fence_for_erasure(principal(), [original.id])
        bound = await uow.semantic_memory.bounds(principal())
        page = await uow.semantic_memory.inventory(
            principal(), after=0, through=bound.creation, limit=1
        )
        assert page.cursor == 1 and page.sources == ()
        changes = await uow.semantic_memory.changes(
            principal(), after=1, through=bound.change, limit=1
        )
        assert changes.changes[0].reason == "erased"
        assert not await uow.semantic_memory.put(principal(), source, VECTOR)
        await uow.memories.purge_erased(principal(), [original.id], operation_id=UUID(int=777))
        assert not await uow.semantic_memory.put(principal(), source, VECTOR)


SCENARIOS.append(pending_put_rolls_back_and_erased_cursor_progresses)


async def semantic_equivalence_recall(
    factory: UnitOfWorkFactory, *, exclude_canonical: bool
) -> None:
    from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
    from agent_core.domain.memory import RecallQuery
    from agent_core.domain.semantic_memory import SemanticQueryVector, query_digest
    from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
    from agent_core.memory.retrieval import HybridMemoryRetriever
    from tests.contract.support import SESSION_ID

    class LocalQuery:
        def lookup(self, query: RecallQuery) -> SemanticQueryVector:
            return SemanticQueryVector(query_sha256=query_digest(query), space=SPACE, vector=VECTOR)

    from tests.contract.reconsolidation_cases import Stores
    from tests.contract.reconsolidation_source_cases import seed

    clock = FixedClock(NOW)
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        plan = await uow.reconsolidation.plan_merge(
            principal(), job.lease_token, group.id, clock.now()
        )
        assert plan is not None
        await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, clock.now()
        )
        member = next(key for key in plan.member_ids if key != plan.canonical_id)
        source = await uow.semantic_memory.capture(principal(), member, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8000, 8010)),
        principal(),
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(update={"semantic_enabled": True}),
        semantic_queries=LocalQuery(),
        reconsolidation_enabled=True,
    )
    query = recall_query(
        text="Produce presentation", exclude_ids=(plan.canonical_id,) if exclude_canonical else ()
    ).model_copy(update={"candidate_selection": "relevance"})
    result = await retriever.recall(query, session_id=SESSION_ID)
    expected = member if exclude_canonical else plan.canonical_id
    assert [item.belief_id for item in result.items] == [expected]
    assert result.items[0].merge_id is not None
    assert result.items[0].arms == ["semantic"]


async def semantic_canonical(factory: UnitOfWorkFactory) -> None:
    await semantic_equivalence_recall(factory, exclude_canonical=False)


async def semantic_excluded_canonical(factory: UnitOfWorkFactory) -> None:
    await semantic_equivalence_recall(factory, exclude_canonical=True)


SCENARIOS.extend([semantic_canonical, semantic_excluded_canonical])
