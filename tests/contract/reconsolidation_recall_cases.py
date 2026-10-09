"""Opt-in merge recall uses original facts and frozen-trace correction semantics."""

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.domain.memory import RecallProfile
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.reconsolidation_cases import Stores
from tests.contract.reconsolidation_source_cases import seed
from tests.contract.support import NOW, SESSION_ID, principal


async def canonical_recall(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        newer = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(newer.model_copy(update={"utility": 1}))
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        canonical = await uow.memories.get(plan.canonical_id, principal())
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
    retriever = HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    snapshot = await retriever.recall(recall_query(), session_id=SESSION_ID, moment="snapshot")
    assert [item.belief_id for item in snapshot.items] == [plan.canonical_id]
    assert snapshot.watermark == operation.store_position
    async with factory() as uow:
        assert (
            await uow.memories.get(newer.id, principal())
        ).evidence_count == newer.evidence_count
        assert await uow.memories.get(plan.canonical_id, principal()) == canonical


RECALL_SCENARIOS = [canonical_recall]


async def undo_corrects_frozen_snapshot(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
    retriever = HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    snapshot = await retriever.recall(recall_query(), session_id=SESSION_ID, moment="snapshot")
    async with factory() as uow:
        before = await uow.traces.get(snapshot.trace_id, principal())
        await uow.reconsolidation.undo_merge(principal(), operation.id, 1, "snapshot", NOW)
    corrections = await retriever.corrections(
        snapshot_id=snapshot.trace_id, watermark=snapshot.watermark
    )
    assert len(corrections) == 1, "undo must correct the grouping recorded by the frozen snapshot"
    assert "equivalence grouping" in corrections[0].render() and "undone" in corrections[0].render()
    assert "no longer holds" not in corrections[0].render()
    delta = await retriever.recall(
        recall_query().model_copy(
            update={
                "min_store_position": snapshot.watermark,
                "text": None,
                "profile": RecallProfile.CORE,
            }
        ),
        session_id=SESSION_ID,
    )
    assert delta.items, "undo makes unchanged original positions eligible for the next-turn delta"
    async with factory() as uow:
        assert await uow.traces.get(snapshot.trace_id, principal()) == before


RECALL_SCENARIOS.append(undo_corrects_frozen_snapshot)


async def visible_fallback_and_disabled_recall(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        newer = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(newer.model_copy(update={"utility": 1}))
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        await uow.reconsolidation.commit_merge(principal(), job.lease_token, group.id, plan, NOW)
    enabled = HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    disabled = HybridMemoryRetriever(factory, FixedClock(NOW), RandomIdFactory(), principal())
    fallback = await enabled.recall(
        recall_query().model_copy(update={"exclude_ids": (plan.canonical_id,)}),
        session_id=SESSION_ID,
    )
    assert [item.belief_id for item in fallback.items] == [newer.id]
    assert fallback.items[0].merge_id is not None
    normal = await disabled.recall(recall_query(), session_id=SESSION_ID)
    assert [item.belief_id for item in normal.items] == [newer.id]
    assert normal.items[0].merge_id is None
    foreign = await enabled.recall(
        recall_query().model_copy(update={"principal_id": "foreign"}), session_id=SESSION_ID
    )
    assert foreign.items == [] and foreign.watermark == 0


async def invalidated_membership_restores_originals(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        await uow.reconsolidation.commit_merge(principal(), job.lease_token, group.id, plan, NOW)
    retriever = HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    snapshot = await retriever.recall(recall_query(), session_id=SESSION_ID, moment="snapshot")
    async with factory() as uow:
        newer = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(
            newer.model_copy(update={"statement": "User prefers detailed answers"})
        )
    delta = await retriever.recall(
        recall_query().model_copy(
            update={
                "min_store_position": snapshot.watermark,
                "text": None,
                "profile": RecallProfile.CORE,
            }
        ),
        session_id=SESSION_ID,
    )
    assert {item.belief_id for item in delta.items} == set(plan.member_ids)
    assert all(item.merge_id is None for item in delta.items)
    corrections = await retriever.corrections(
        snapshot_id=snapshot.trace_id, watermark=snapshot.watermark
    )
    assert len(corrections) == 1 and corrections[0].kind == "merge_invalidated"


RECALL_SCENARIOS.extend(
    [visible_fallback_and_disabled_recall, invalidated_membership_restores_originals]
)


async def canonical_outside_candidate_cap(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        for i in range(80):
            await uow.memories.upsert_belief(
                memory(
                    belief_id=600 + i, statement=f"User prefers concise answer variant {i}"
                ).model_copy(
                    update={
                        "confidence": 0.01,
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
        newer = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(
            newer.model_copy(
                update={"utility": 1, "store_position": await uow.memories.next_position()}
            )
        )
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        await uow.reconsolidation.commit_merge(principal(), job.lease_token, group.id, plan, NOW)
    retriever = HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    for query in (
        recall_query().model_copy(update={"max_items": 1}),
        recall_query().model_copy(update={"max_items": 1, "as_of": NOW, "known_at": NOW}),
    ):
        result = await retriever.recall(query, session_id=SESSION_ID)
        assert [item.belief_id for item in result.items] == [plan.canonical_id]


async def invalidation_does_not_render_erased_or_hidden_sources(factory: UnitOfWorkFactory) -> None:
    from agent_core.domain.memory import Sensitivity

    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
        original = await uow.memories.get(plan.canonical_id, principal())
        await uow.memories.reinforce(
            original.model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
        )
    retriever = HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    query = recall_query().model_copy(
        update={
            "sensitivity_ceiling": Sensitivity.INTERNAL,
            "min_store_position": operation.store_position,
            "profile": RecallProfile.CORE,
            "text": None,
        }
    )
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [memory(belief_id=502).id]
    assert result.items[0].merge_id is None
    async with factory() as uow:
        await uow.memories.fence_for_erasure(principal(), [memory(belief_id=502).id])
    assert (await retriever.recall(query, session_id=SESSION_ID)).items == []


async def expired_support_invalidates_frozen_group(factory: UnitOfWorkFactory) -> None:
    from datetime import timedelta

    from tests.contract.reconsolidation_source_cases import queue_sources, seed_sources

    async with factory() as uow:
        stores = Stores(uow.memories, uow.reconsolidation, uow.events, uow.people)
        await seed_sources(stores)
        for key in (memory().id, memory(belief_id=502).id):
            record = await uow.memories.get(key, principal())
            await uow.memories.reinforce(
                record.model_copy(update={"expires_at": NOW + timedelta(minutes=1)})
            )
        job, group = await queue_sources(stores)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        await uow.reconsolidation.commit_merge(principal(), job.lease_token, group.id, plan, NOW)
    clock = FixedClock(NOW)
    retriever = HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    snapshot = await retriever.recall(recall_query(), session_id=SESSION_ID, moment="snapshot")
    clock.advance(timedelta(minutes=2))
    corrections = await retriever.corrections(
        snapshot_id=snapshot.trace_id, watermark=snapshot.watermark
    )
    assert [item.kind for item in corrections] == ["merge_invalidated"]
    async with factory() as uow:
        assert await uow.memories.head_position(principal()) > snapshot.watermark


RECALL_SCENARIOS.extend(
    [
        canonical_outside_candidate_cap,
        invalidation_does_not_render_erased_or_hidden_sources,
        expired_support_invalidates_frozen_group,
    ]
)


async def merge_preserves_best_member_rank_under_pressure(factory: UnitOfWorkFactory) -> None:
    async with factory() as uow:
        job, group = await seed(Stores(uow.memories, uow.reconsolidation, uow.events, uow.people))
        newer = await uow.memories.get(memory(belief_id=502).id, principal())
        await uow.memories.reinforce(newer.model_copy(update={"utility": 1.0}))
        distractor = memory(belief_id=503, statement="User prefers illustrated answers").model_copy(
            update={"utility": 0.5, "store_position": await uow.memories.next_position()}
        )
        await uow.memories.upsert_belief(distractor)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        await uow.reconsolidation.commit_merge(principal(), job.lease_token, group.id, plan, NOW)
    query = recall_query().model_copy(
        update={"text": None, "profile": RecallProfile.CORE, "max_items": 1}
    )
    baseline = await HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal()
    ).recall(query, session_id=SESSION_ID)
    merged = await HybridMemoryRetriever(
        factory, FixedClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    ).recall(query, session_id=SESSION_ID)
    assert baseline.items[0].belief_id == newer.id
    assert [item.statement for item in merged.items] == [item.statement for item in baseline.items]
    assert merged.items[0].belief_id == plan.canonical_id
    async with factory() as uow:
        canonical = await uow.memories.get(plan.canonical_id, principal())
        assert canonical.utility == 0, "ranking must not reinforce the canonical's stored evidence"


RECALL_SCENARIOS.append(merge_preserves_best_member_rank_under_pressure)
