"""Extractive projections share source admission and transactional erasure."""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import MemoryRecord, Portability, Sensitivity
from agent_core.domain.reconsolidation import ReconsolidationGroup, ReconsolidationJob
from agent_core.domain.reconsolidation_operations import StoredSummary
from agent_core.domain.reconsolidation_summary import PreparedSummary, SummaryClause, SummaryMemory
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.reconsolidation_source_cases import queue_sources, seed_sources
from tests.contract.support import NOW, principal


async def summary_inputs(
    uow: Stores, *, expires: bool = False, local: bool = False
) -> tuple[
    ReconsolidationJob, ReconsolidationGroup, tuple[MemoryRecord, ...], tuple[SummaryClause, ...]
]:
    await seed_sources(uow)
    second = await uow.memories.get(memory(belief_id=502).id, principal())
    await uow.memories.reinforce(
        second.model_copy(update={"statement": "I prefer local execution."})
    )
    for key in (501, 502):
        original = await uow.memories.get(UUID(int=key), principal())
        await uow.memories.reinforce(
            original.model_copy(
                update={
                    "expires_at": NOW + timedelta(seconds=30) if expires else original.expires_at,
                    "scope": "project:alpha" if local else original.scope,
                    "portability": Portability.LOCAL if local else original.portability,
                }
            )
        )
    job, group = await queue_sources(uow)
    originals = tuple([await uow.memories.get(s.belief_id, principal()) for s in group.sources])
    clauses = tuple(SummaryClause(text=r.statement, source_ids=(r.id,)) for r in originals)
    return job, group, originals, clauses


async def committed_summary(
    factory: Factory, *, omitted: bool = False, expires: bool = False, local: bool = False
) -> StoredSummary:
    async with factory() as uow:
        job, group, _, clauses = await summary_inputs(uow, expires=expires, local=local)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses[:1] if omitted else clauses, NOW
        )
        assert prepared is not None
        return await uow.reconsolidation.commit_summary(
            principal(), job.lease_token, group.id, prepared, NOW
        )


async def read_summary(uow: Stores, value: StoredSummary) -> SummaryMemory | None:
    return await uow.reconsolidation.get_summary(
        principal(), value.id, NOW, ceiling=Sensitivity.INTERNAL, current_scope="user"
    )


async def summary_preserves_originals(factory: Factory) -> None:
    async with factory() as uow:
        job, group, originals, clauses = await summary_inputs(uow)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None, "distinct supported clauses need an extractive summary plan"
        assert prepared.plan.member_ids == tuple(r.id for r in originals)
        value = await uow.reconsolidation.commit_summary(
            principal(), job.lease_token, group.id, prepared, NOW
        )
    async with factory() as uow:
        projection = await read_summary(uow, value)
        assert projection is not None and projection.content == prepared
        assert projection.model_dump().get("revision") == 1, "derived projections need a revision"
        assert all(c.text not in value.model_dump_json() for c in clauses)
        for original in originals:
            assert await uow.memories.get(original.id, principal()) == original
        assert await uow.reconsolidation.get_merge(principal(), value.id, NOW) is None
        assert (
            await uow.reconsolidation.active_merges(principal(), prepared.plan.member_ids, NOW)
            == ()
        )
        assert (
            await uow.reconsolidation.merges_at(
                principal(), prepared.plan.member_ids, as_of=NOW, known_at=NOW
            )
            == ()
        )
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        assert value.id not in {s.belief_id for s in page.sources}
        with pytest.raises(ConflictError):
            await uow.reconsolidation.commit_summary(
                principal(), job.lease_token, group.id, prepared, NOW
            )


async def summary_omitted_source_invalidation(factory: Factory) -> None:
    value = await committed_summary(factory, omitted=True)
    assert value.plan.omitted_source_ids == (UUID(int=502),)
    async with factory() as uow:
        original = await uow.memories.get(UUID(int=502), principal())
        await uow.memories.reinforce(
            original.model_copy(update={"statement": "I prefer cloud execution."})
        )
    async with factory() as uow:
        assert await read_summary(uow, value) is None


async def summary_erasure_and_rollback(factory: Factory) -> None:
    value = await committed_summary(factory)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.memories.fence_for_erasure(principal(), [UUID(int=501)])
            assert await read_summary(uow, value) is None
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await read_summary(uow, value) is not None
        await uow.memories.fence_for_erasure(principal(), [UUID(int=501)])
    async with factory() as uow:
        assert await read_summary(uow, value) is None


async def summary_commit_rollback(factory: Factory) -> None:
    async with factory() as uow:
        job, group, _, clauses = await summary_inputs(uow)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            first = await uow.reconsolidation.commit_summary(
                principal(), job.lease_token, group.id, prepared, NOW
            )
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await read_summary(uow, first) is None
        committed = await uow.reconsolidation.commit_summary(
            principal(), job.lease_token, group.id, prepared, NOW
        )
        assert await read_summary(uow, committed) is not None


async def summary_scope_ceiling_and_owner(factory: Factory) -> None:
    value = await committed_summary(factory, local=True)
    async with factory() as uow:
        for owner, ceiling, scope in (
            (principal(), Sensitivity.INTERNAL, "project:other"),
            (principal(), Sensitivity.PUBLIC, "project:alpha"),
            (
                principal().model_copy(update={"principal_id": "other"}),
                Sensitivity.INTERNAL,
                "project:alpha",
            ),
            (
                principal().model_copy(update={"tenant_id": "other"}),
                Sensitivity.INTERNAL,
                "project:alpha",
            ),
        ):
            assert (
                await uow.reconsolidation.get_summary(
                    owner, value.id, NOW, ceiling=ceiling, current_scope=scope
                )
                is None
            )
        assert (
            await uow.reconsolidation.get_summary(
                principal(),
                value.id,
                NOW,
                ceiling=Sensitivity.INTERNAL,
                current_scope="project:alpha",
            )
            is not None
        )


async def summary_expiry_revalidation(factory: Factory) -> None:
    value = await committed_summary(factory, expires=True)
    async with factory() as uow:
        assert await read_summary(uow, value) is not None
        assert (
            await uow.reconsolidation.get_summary(
                principal(),
                value.id,
                NOW + timedelta(seconds=30),
                ceiling=Sensitivity.INTERNAL,
                current_scope="user",
            )
            is None
        )


async def summary_stale_inputs_and_lease(factory: Factory) -> None:
    async with factory() as uow:
        job, group, originals, clauses = await summary_inputs(uow)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
        await uow.memories.reinforce(originals[0].model_copy(update={"confidence": 0.2}))
    async with factory() as uow:
        for token, at in (
            (job.lease_token, NOW),
            (UUID(int=4444), NOW),
            (job.lease_token, NOW + timedelta(seconds=121)),
        ):
            with pytest.raises(ConflictError):
                await uow.reconsolidation.commit_summary(principal(), token, group.id, prepared, at)


async def summary_rejects_fabricated_plan(factory: Factory) -> None:
    async with factory() as uow:
        job, group, _, clauses = await summary_inputs(uow)
        prepared = await uow.reconsolidation.plan_summary(
            principal(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
        forged: PreparedSummary = prepared.model_copy(update={"confidence": 1.0})
        with pytest.raises(ConflictError):
            await uow.reconsolidation.commit_summary(
                principal(), job.lease_token, group.id, forged, NOW
            )
        invented = (clauses[0].model_copy(update={"text": "Always use local execution."}),)
        assert (
            await uow.reconsolidation.plan_summary(
                principal(), job.lease_token, group.id, invented, NOW
            )
            is None
        )


async def summary_usage_preserves_support(factory: Factory) -> None:
    value = await committed_summary(factory)
    async with factory() as uow:
        before = await read_summary(uow, value)
        original = await uow.memories.get(UUID(int=501), principal())
        await uow.memories.reinforce(
            original.model_copy(update={"utility": 0.1, "last_used_at": NOW})
        )
        assert await read_summary(uow, value) == before


SUMMARY_SCENARIOS: list[Callable[[Factory], Awaitable[None]]] = [
    summary_preserves_originals,
    summary_omitted_source_invalidation,
    summary_erasure_and_rollback,
    summary_commit_rollback,
    summary_scope_ceiling_and_owner,
    summary_expiry_revalidation,
    summary_stale_inputs_and_lease,
    summary_rejects_fabricated_plan,
    summary_usage_preserves_support,
]


async def summary_all_reverse_dependencies(factory: Factory) -> None:
    async with factory() as uow:
        await seed_sources(uow)
        await uow.memories.upsert_belief(
            memory(belief_id=503).model_copy(
                update={
                    "statement": "I prefer local execution.",
                    "store_position": await uow.memories.next_position(),
                }
            )
        )
        job = await uow.reconsolidation.claim_due(principal(), NOW, "summary-dependencies")
        assert job is not None
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        await uow.reconsolidation.checkpoint(
            principal(), job.lease_token, page, (page.sources[:2], page.sources[1:]), NOW
        )
        values = []
        for _ in range(2):
            group = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
            assert group is not None
            record = await uow.memories.get(group.sources[0].belief_id, principal())
            prepared = await uow.reconsolidation.plan_summary(
                principal(),
                job.lease_token,
                group.id,
                (SummaryClause(text=record.statement, source_ids=(record.id,)),),
                NOW,
            )
            assert prepared is not None
            values.append(
                await uow.reconsolidation.commit_summary(
                    principal(), job.lease_token, group.id, prepared, NOW
                )
            )
        assert all([await read_summary(uow, v) is not None for v in values])
        await uow.memories.fence_for_erasure(principal(), [UUID(int=502)])
        assert all([await read_summary(uow, v) is None for v in values])


async def summary_people_change(factory: Factory) -> None:
    from tests.contract.reconsolidation_attribution_cases import source

    value = await committed_summary(factory)
    async with factory() as uow, uow.people.lock(principal()):
        await uow.people.put(source().model_copy(update={"excluded": True}), expected_revision=0)
    async with factory() as uow:
        assert await read_summary(uow, value) is None


SUMMARY_SCENARIOS.extend([summary_all_reverse_dependencies, summary_people_change])
