"""Durable, reversible equivalence groups across the real UOW boundary."""

import asyncio
from datetime import timedelta

import pytest

from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.reconsolidation import ReconsolidationGroup, ReconsolidationJob
from agent_core.domain.reconsolidation_operations import StoredMerge
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_source_cases import queue_sources, seed, seed_sources
from tests.contract.support import NOW, principal


async def durable_commit_and_undo(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        originals = tuple([await uow.memories.get(key, principal()) for key in plan.member_ids])
        operation = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
        assert operation is not None, "an authenticated equivalent group must persist a merge"
        assert operation.state == "committed" and operation.revision == 1
        assert operation.store_position > max(original.store_position for original in originals)
        assert operation.plan == plan and operation.group_id == group.id
    async with factory() as uow:
        assert await uow.reconsolidation.get_merge(principal(), operation.id, NOW) == operation
        assert (
            await uow.reconsolidation.merge_members(principal(), operation.id, NOW)
            == plan.member_ids
        )
        for original in originals:
            assert await uow.memories.get(original.id, principal()) == original
        undone = await uow.reconsolidation.undo_merge(principal(), operation.id, 1, "undo-key", NOW)
        assert undone is not None and undone.state == "undone" and undone.revision == 2
        assert undone.store_position > operation.store_position
    async with factory() as uow:
        assert await uow.reconsolidation.get_merge(principal(), operation.id, NOW) == undone
        assert await uow.reconsolidation.merge_members(principal(), operation.id, NOW) == ()
        assert (
            await uow.reconsolidation.undo_merge(principal(), operation.id, 1, "undo-key", NOW)
            == undone
        )


OPERATION_SCENARIOS = [durable_commit_and_undo]


async def membership_transitions_advance_recall_watermark(factory: Factory) -> None:
    _, _, value = await committed(factory)
    async with factory() as uow:
        assert await uow.memories.head_position(principal()) == value.store_position
        assert (
            await uow.memories.head_position(
                principal().model_copy(update={"principal_id": "another-owner"})
            )
            == 0
        )
        undone = await uow.reconsolidation.undo_merge(principal(), value.id, 1, "delta", NOW)
        assert await uow.memories.head_position(principal()) == undone.store_position


OPERATION_SCENARIOS.append(membership_transitions_advance_recall_watermark)


async def committed(
    factory: Factory,
) -> tuple[ReconsolidationJob, ReconsolidationGroup, StoredMerge]:
    async with factory() as uow:
        job, group = await seed(uow)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        value = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
        assert value is not None
        return job, group, value


async def commit_and_undo_rollback(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            aborted = await uow.reconsolidation.commit_merge(
                principal(), job.lease_token, group.id, plan, NOW
            )
            assert aborted is not None
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await uow.reconsolidation.get_merge(principal(), aborted.id, NOW) is None
        assert (
            await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
            == plan
        )
        value = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
        assert value is not None
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.reconsolidation.undo_merge(principal(), value.id, 1, "aborted-undo", NOW)
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await uow.reconsolidation.get_merge(principal(), value.id, NOW) == value
        assert (
            await uow.reconsolidation.merge_members(principal(), value.id, NOW) == plan.member_ids
        )
        assert (
            await uow.reconsolidation.undo_merge(principal(), value.id, 1, "aborted-undo", NOW)
            is not None
        )


async def changed_support_invalidates(factory: Factory) -> None:
    _, _, value = await committed(factory)
    async with factory() as uow:
        original = await uow.memories.get(memory().id, principal())
        await uow.memories.reinforce(original.model_copy(update={"confidence": 0.6}))
    async with factory() as uow:
        current = await uow.reconsolidation.get_merge(principal(), value.id, NOW)
        assert current is not None and current.state == "invalidated"
        assert current.revision == 2 and current.store_position > value.store_position
        assert await uow.reconsolidation.merge_members(principal(), value.id, NOW) == ()
        with pytest.raises(ConflictError):
            await uow.reconsolidation.undo_merge(principal(), value.id, 1, "too-late", NOW)
        assert await uow.memories.get(original.id, principal()) == original.model_copy(
            update={"confidence": 0.6}
        )


async def usage_preserves_merge(factory: Factory) -> None:
    _, _, value = await committed(factory)
    async with factory() as uow:
        original = await uow.memories.get(memory().id, principal())
        await uow.memories.reinforce(
            original.model_copy(update={"utility": 0.1, "last_used_at": NOW})
        )
    async with factory() as uow:
        assert await uow.reconsolidation.get_merge(principal(), value.id, NOW) == value


async def invalidation_rolls_back(factory: Factory) -> None:
    _, _, value = await committed(factory)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.memories.fence_for_erasure(principal(), [memory().id])
            current = await uow.reconsolidation.get_merge(principal(), value.id, NOW)
            assert current is not None and current.state == "invalidated"
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await uow.reconsolidation.get_merge(principal(), value.id, NOW) == value


async def undo_replay_after_erasure(factory: Factory) -> None:
    _, _, value = await committed(factory)
    async with factory() as uow:
        undone = await uow.reconsolidation.undo_merge(principal(), value.id, 1, "stable", NOW)
    async with factory() as uow:
        await uow.memories.fence_for_erasure(principal(), list(value.plan.member_ids))
    async with factory() as uow:
        assert (
            await uow.reconsolidation.undo_merge(principal(), value.id, 1, "stable", NOW) == undone
        )
        for revision, key in ((2, "stable"), (1, "different")):
            with pytest.raises(ConflictError):
                await uow.reconsolidation.undo_merge(principal(), value.id, revision, key, NOW)
        with pytest.raises(NotFoundError):
            await uow.memories.get(memory().id, principal())


async def owner_isolation(factory: Factory) -> None:
    job, group, value = await committed(factory)
    other = principal().model_copy(update={"principal_id": "foreign-owner"})
    async with factory() as uow:
        assert await uow.reconsolidation.get_merge(other, value.id, NOW) is None
        assert await uow.reconsolidation.merge_members(other, value.id, NOW) == ()
        with pytest.raises(NotFoundError):
            await uow.reconsolidation.undo_merge(other, value.id, 1, "foreign", NOW)
        with pytest.raises(NotFoundError):
            await uow.reconsolidation.commit_merge(
                other, job.lease_token, group.id, value.plan, NOW
            )


async def stale_preview_refused(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
    async with factory() as uow:
        original = await uow.memories.get(memory().id, principal())
        await uow.memories.reinforce(original.model_copy(update={"subject": "changed"}))
    async with factory() as uow:
        with pytest.raises(ConflictError):
            await uow.reconsolidation.commit_merge(
                principal(), job.lease_token, group.id, plan, NOW
            )


async def undo_blocks_recreated_inputs(factory: Factory) -> None:
    job, _, value = await committed(factory)
    async with factory() as uow:
        await uow.reconsolidation.undo_merge(principal(), value.id, 1, "block", NOW)
        for i in range(2):
            await uow.memories.upsert_belief(
                memory(belief_id=601 + i).model_copy(
                    update={
                        "store_position": await uow.memories.next_position(),
                        "subject": "renamed",
                        "consolidation_policy_version": "different-policy",
                    }
                )
            )
        await uow.reconsolidation.release(principal(), job.lease_token, NOW)
        tomorrow = NOW + timedelta(days=1)
        lease = await uow.reconsolidation.claim_due(principal(), tomorrow, "tomorrow")
        assert lease is not None
        page = await uow.reconsolidation.inventory(principal(), lease.lease_token, tomorrow)
        sources = tuple(s for s in page.sources if s.belief_id.int in (601, 602))
        await uow.reconsolidation.checkpoint(
            principal(), lease.lease_token, page, (sources,), tomorrow
        )
        group = await uow.reconsolidation.claim_group(principal(), lease.lease_token, tomorrow)
        assert group is not None
        assert (
            await uow.reconsolidation.plan_merge(principal(), lease.lease_token, group.id, tomorrow)
            is None
        )


async def deadline_refuses_commit(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        with pytest.raises(ConflictError, match="deadline"):
            await uow.reconsolidation.commit_merge(
                principal(), job.lease_token, group.id, plan, NOW + timedelta(seconds=120)
            )
        assert (
            await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
            == plan
        )


OPERATION_SCENARIOS += [
    commit_and_undo_rollback,
    changed_support_invalidates,
    usage_preserves_merge,
    invalidation_rolls_back,
    undo_replay_after_erasure,
    owner_isolation,
    stale_preview_refused,
    undo_blocks_recreated_inputs,
    deadline_refuses_commit,
]


async def claims_bounded_across_retries(factory: Factory) -> None:
    async with factory() as uow:
        for i in range(4):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + i).model_copy(update={"store_position": i + 1})
            )
        job = await uow.reconsolidation.claim_due(principal(), NOW, "bounded")
        assert job is not None
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        sources = tuple(sorted(page.sources, key=lambda s: s.belief_id))
        await uow.reconsolidation.checkpoint(
            principal(), job.lease_token, page, (sources[:2], sources[2:]), NOW
        )
        for _ in range(4):
            group = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
            assert group is not None
            await uow.reconsolidation.finish_group(
                principal(), job.lease_token, group.id, "retry", NOW
            )
        assert await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW) is None, (
            "a fifth claim must wait for a fresh slice"
        )
        ready = await uow.reconsolidation.release(principal(), job.lease_token, NOW)
        assert ready.claimed_groups == 4 and ready.state == "ready"
        next_slice = await uow.reconsolidation.claim_due(principal(), NOW, "next-slice")
        assert next_slice is not None and next_slice.claimed_groups == 0
        assert (
            await uow.reconsolidation.claim_group(principal(), next_slice.lease_token, NOW)
            is not None
        )


OPERATION_SCENARIOS += [claims_bounded_across_retries]


async def racing_commits_have_one_winner(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None

    async def commit() -> StoredMerge | None:
        try:
            async with factory() as uow:
                return await uow.reconsolidation.commit_merge(
                    principal(), job.lease_token, group.id, plan, NOW
                )
        except ConflictError:
            return None

    results = await asyncio.gather(commit(), commit())
    assert sum(value is not None for value in results) == 1


async def source_mutation_waits_for_commit(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
    committed_event, release = asyncio.Event(), asyncio.Event()

    async def commit() -> StoredMerge:
        async with factory() as uow:
            value = await uow.reconsolidation.commit_merge(
                principal(), job.lease_token, group.id, plan, NOW
            )
            committed_event.set()
            await release.wait()
            return value

    async def mutate() -> None:
        async with factory() as uow:
            await uow.memories.fence_for_erasure(principal(), [memory().id])

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        writing = tasks.create_task(commit())
        await committed_event.wait()
        mutation = tasks.create_task(mutate())
        try:
            await asyncio.sleep(0.05)
            assert not mutation.done(), "source mutation must wait for the atomic merge"
        finally:
            release.set()
    async with factory() as uow:
        value = await uow.reconsolidation.get_merge(principal(), writing.result().id, NOW)
        assert value is not None and value.state == "invalidated"
        assert await uow.reconsolidation.merge_members(principal(), value.id, NOW) == ()


async def expiry_rechecked_on_membership_read(factory: Factory) -> None:
    async with factory() as uow:
        await seed_sources(uow)
        for key in (501, 502):
            original = await uow.memories.get(memory(belief_id=key).id, principal())
            await uow.memories.reinforce(
                original.model_copy(update={"expires_at": NOW + timedelta(seconds=10)})
            )
        job, group = await queue_sources(uow)
        plan = await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
        assert plan is not None
        value = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, group.id, plan, NOW
        )
    async with factory() as uow:
        assert (
            await uow.reconsolidation.merge_members(
                principal(), value.id, NOW + timedelta(seconds=10)
            )
            == ()
        )
        current = await uow.reconsolidation.get_merge(
            principal(), value.id, NOW + timedelta(seconds=10)
        )
        assert current is not None and current.state == "invalidated"


async def overlapping_membership_refused(factory: Factory) -> None:
    async with factory() as uow:
        await seed_sources(uow)
        await uow.memories.upsert_belief(
            memory(belief_id=503).model_copy(
                update={"store_position": await uow.memories.next_position()}
            )
        )
        job = await uow.reconsolidation.claim_due(principal(), NOW, "overlap")
        assert job is not None
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        sources = tuple(sorted(page.sources, key=lambda s: s.belief_id))
        await uow.reconsolidation.checkpoint(
            principal(), job.lease_token, page, (sources[:2], sources[1:]), NOW
        )
        first = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
        second = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
        assert first is not None and second is not None
        first_plan = await uow.reconsolidation.plan_merge(
            principal(), job.lease_token, first.id, NOW
        )
        second_plan = await uow.reconsolidation.plan_merge(
            principal(), job.lease_token, second.id, NOW
        )
        assert first_plan is not None and second_plan is not None
        value = await uow.reconsolidation.commit_merge(
            principal(), job.lease_token, first.id, first_plan, NOW
        )
        with pytest.raises(ConflictError):
            await uow.reconsolidation.commit_merge(
                principal(), job.lease_token, second.id, second_plan, NOW
            )
        assert (
            await uow.reconsolidation.merge_members(principal(), value.id, NOW)
            == first_plan.member_ids
        )


OPERATION_SCENARIOS += [
    racing_commits_have_one_winner,
    source_mutation_waits_for_commit,
    expiry_rechecked_on_membership_read,
    overlapping_membership_refused,
]


async def active_membership_lookup_is_bounded_and_owner_scoped(factory: Factory) -> None:
    _, _, operation = await committed(factory)
    async with factory() as uow:
        assert await uow.reconsolidation.active_merges(
            principal(), operation.plan.member_ids, NOW
        ) == (operation,)
        assert (
            await uow.reconsolidation.active_merges(
                principal().model_copy(update={"principal_id": "another-owner"}),
                operation.plan.member_ids,
                NOW,
            )
            == ()
        )
        with pytest.raises(ValueError, match="1000"):
            await uow.reconsolidation.active_merges(
                principal(), (operation.plan.canonical_id,) * 1001, NOW
            )
        await uow.memories.fence_for_erasure(principal(), [operation.plan.canonical_id])
        assert (
            await uow.reconsolidation.active_merges(principal(), operation.plan.member_ids, NOW)
            == ()
        )


OPERATION_SCENARIOS.append(active_membership_lookup_is_bounded_and_owner_scoped)
