"""Attribution changes and content-free erasure metadata share the memory UOW."""

import asyncio
from uuid import UUID

import pytest

from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleSource, Person, PersonMemoryLink, PersonMention
from agent_core.domain.reconsolidation import ReconsolidationJob, SourceVersion
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.reconsolidation_source_cases import seed
from tests.contract.support import NOW, SESSION_ID, principal


def person(key: int = 801) -> Person:
    return Person(
        id=UUID(int=key),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        created_at=NOW,
        updated_at=NOW,
        sensitivity=Sensitivity.INTERNAL,
        display_name="Alice",
        state="active",
    )


def link(belief: int = 501) -> PersonMemoryLink:
    return PersonMemoryLink(
        id=UUID(int=811),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        created_at=NOW,
        updated_at=NOW,
        sensitivity=Sensitivity.INTERNAL,
        person_id=person().id,
        belief_id=UUID(int=belief),
    )


async def unrelated_purge_does_not_disable_owner(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        async with uow.people.lock(principal()):
            await uow.people.put(person(), expected_revision=0)
            await uow.people.fence_for_erasure(principal(), [person().id])
            await uow.people.purge_erased(principal())
    async with factory() as uow:
        assert (
            await uow.reconsolidation.plan_merge(principal(), job.lease_token, group.id, NOW)
            is not None
        ), "unrelated opaque tombstones must not disable an owner's entire bank"
        assert (
            await uow.people.get(principal(), person().id, ceiling=Sensitivity.RESTRICTED) is None
        )


async def linked_person_change_advances_source_revision(factory: Factory) -> None:
    async with factory() as uow:
        for i in range(2):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + i).model_copy(update={"store_position": i + 1})
            )
        async with uow.people.lock(principal()):
            await uow.people.put(person(), expected_revision=0)
            await uow.people.put(link(), expected_revision=0)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
    async with factory() as uow, uow.people.lock(principal()):
        await uow.people.put(
            person().model_copy(update={"revision": 2, "display_name": "Alice Smith"}),
            expected_revision=1,
        )
    async with factory() as uow:
        after = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        old, new = ({s.belief_id: s for s in page.sources} for page in (before, after))
        assert new[UUID(int=501)].content_revision == old[UUID(int=501)].content_revision + 1, (
            "identity changes must fence already selected original revisions"
        )
        assert new[UUID(int=502)] == old[UUID(int=502)]
        assert await uow.memories.get(memory().id, principal()) == memory()


async def attribution_mutation_rolls_back(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        async with uow.people.lock(principal()):
            await uow.people.put(person(), expected_revision=0)
            await uow.people.put(link(), expected_revision=0)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow, uow.people.lock(principal()):
            await uow.people.put(
                person().model_copy(update={"revision": 2, "display_name": "Aborted"}),
                expected_revision=1,
            )
            raise RuntimeError("abort")
    async with factory() as uow:
        assert (
            await uow.people.get(principal(), person().id, ceiling=Sensitivity.INTERNAL) == person()
        ), "attribution and original revisions must roll back together"
        assert await uow.reconsolidation.inventory(principal(), job.lease_token, NOW) == before


def source(sequence: int = 1) -> PeopleSource:
    return PeopleSource(
        id=UUID(int=820 + sequence),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        created_at=NOW,
        updated_at=NOW,
        sensitivity=Sensitivity.INTERNAL,
        session_id=SESSION_ID,
        event_sequence=sequence,
        source_kind="owner",
        evidence_at=NOW,
        source_revision="source@1",
        copy_group="a" * 64,
    )


async def versions(uow: Stores, job: ReconsolidationJob) -> dict[UUID, SourceVersion]:
    page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
    return {item.belief_id: item for item in page.sources}


async def link_reassignment_fences_both_originals(factory: Factory) -> None:
    async with factory() as uow:
        for index in range(3):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + index).model_copy(update={"store_position": index + 1})
            )
        await uow.people.put(person(), expected_revision=0)
        await uow.people.put(link(), expected_revision=0)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await versions(uow, job)
        await uow.people.put(link(502).model_copy(update={"revision": 2}), expected_revision=1)
        after = await versions(uow, job)
        for key in (501, 502):
            assert (
                after[UUID(int=key)].content_revision == before[UUID(int=key)].content_revision + 1
            )
        assert after[UUID(int=503)] == before[UUID(int=503)]


async def mention_role_fences_only_its_event(factory: Factory) -> None:
    async with factory() as uow:
        for index in range(2):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + index).model_copy(
                    update={"store_position": index + 1, "source_event_ids": [index + 1]}
                )
            )
        await uow.people.put(source(), expected_revision=0)
        mention = PersonMention(
            id=UUID(int=830),
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            created_at=NOW,
            updated_at=NOW,
            sensitivity=Sensitivity.INTERNAL,
            source_id=source().id,
            support_ids=[source().id],
            start=0,
            end=1,
        )
        await uow.people.put(mention, expected_revision=0)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await versions(uow, job)
        await uow.people.put(
            mention.model_copy(update={"revision": 2, "role": "speaker"}), expected_revision=1
        )
        after = await versions(uow, job)
        assert after[UUID(int=501)].content_revision == before[UUID(int=501)].content_revision + 1
        assert after[UUID(int=502)] == before[UUID(int=502)]


async def purge_is_idempotent_and_rolls_back(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        await uow.people.put(person(), expected_revision=0)
        await uow.people.put(link(), expected_revision=0)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await versions(uow, job)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.people.fence_for_erasure(principal(), [person().id])
            await uow.people.purge_erased(principal())
            raise RuntimeError("abort")
    async with factory() as uow:
        assert await uow.people.get(principal(), link().id, ceiling=Sensitivity.INTERNAL) == link()
        assert await versions(uow, job) == before
        await uow.people.fence_for_erasure(principal(), [person().id])
        fenced = await versions(uow, job)
        assert fenced[memory().id].content_revision == before[memory().id].content_revision + 1
        await uow.people.purge_erased(principal())
        await uow.people.fence_for_erasure(principal(), [person().id])
        assert await versions(uow, job) == fenced
        assert (
            await uow.people.memory_attribution_records(principal(), memory().id, (source().id,))
            is None
        )
        assert await uow.memories.get(memory().id, principal()) == memory()


async def copy_rebase_fences_surviving_link(factory: Factory) -> None:
    async with factory() as uow:
        # The linked original uses event 3; erasing event 1 only reaches it
        # through the person's changed support, not through its own provenance.
        await uow.memories.upsert_belief(memory().model_copy(update={"source_event_ids": [3]}))
        for sequence in (1, 2):
            await uow.people.put(source(sequence), expected_revision=0)
        backed = person().model_copy(update={"support_ids": [source().id]})
        await uow.people.put(backed, expected_revision=0)
        await uow.people.put(link(), expected_revision=0)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await versions(uow, job)
        await uow.people.erase(principal(), [source().id], preserve_independent=True)
        current = await uow.people.get(principal(), person().id, ceiling=Sensitivity.INTERNAL)
        assert current is not None and current.support_ids == [source(2).id]
        after = await versions(uow, job)
        assert after[memory().id].content_revision == before[memory().id].content_revision + 1, (
            "a surviving identity's changed support must fence linked originals"
        )


async def purged_link_stops_following_person_changes(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        await uow.people.put(person(), expected_revision=0)
        await uow.people.put(link(), expected_revision=0)
        await uow.people.erase(principal(), [link().id])
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await versions(uow, job)
        await uow.people.put(
            person().model_copy(update={"revision": 2, "display_name": "Alice Smith"}),
            expected_revision=1,
        )
        assert await versions(uow, job) == before, "a purged link is no longer a current dependency"
        assert (
            await uow.people.memory_attribution_records(principal(), memory().id, (source().id,))
            is None
        )


async def attribution_write_waits_for_original_transaction(factory: Factory) -> None:
    held, release = asyncio.Event(), asyncio.Event()

    async def original_writer() -> None:
        async with factory() as uow:
            await uow.memories.upsert_belief(memory())
            held.set()
            await release.wait()

    async def attribution_writer() -> None:
        async with factory() as uow:
            await uow.people.put(person(), expected_revision=0)

    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        tasks.create_task(original_writer())
        await held.wait()
        writer = tasks.create_task(attribution_writer())
        try:
            await asyncio.sleep(0.05)
            assert not writer.done(), "People writes must serialize with original-memory commits"
        finally:
            release.set()


async def source_index_follows_commits_and_rollback(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        await uow.people.put(source(), expected_revision=0)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await versions(uow, job)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.memories.reinforce(memory().model_copy(update={"source_event_ids": [2]}))
            raise RuntimeError("abort")
    async with factory() as uow:
        await uow.people.put(
            source().model_copy(update={"revision": 2, "source_revision": "source@2"}),
            expected_revision=1,
        )
        after = await versions(uow, job)
        assert after[memory().id].content_revision == before[memory().id].content_revision + 1
        assert await uow.memories.get(memory().id, principal()) == memory()
        await uow.memories.reinforce(memory().model_copy(update={"source_event_ids": [2]}))
        moved = await versions(uow, job)
        await uow.people.put(
            source().model_copy(update={"revision": 3, "source_revision": "source@3"}),
            expected_revision=2,
        )
        assert await versions(uow, job) == moved


async def principal_erasure_fences_attribution_and_rolls_back(factory: Factory) -> None:
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
        await uow.people.put(person(), expected_revision=0)
        await uow.people.put(link(), expected_revision=0)
        job = await uow.reconsolidation.claim_due(principal(), NOW, "attribution")
        assert job is not None
        before = await versions(uow, job)
    with pytest.raises(RuntimeError, match="abort"):
        async with factory() as uow:
            await uow.people.erase_principal(principal())
            raise RuntimeError("abort")
    async with factory() as uow:
        assert (
            await uow.people.get(principal(), person().id, ceiling=Sensitivity.INTERNAL) == person()
        )
        assert await versions(uow, job) == before
        await uow.people.erase_principal(principal())
        after = await versions(uow, job)
        assert after[memory().id].content_revision == before[memory().id].content_revision + 1, (
            "removing all owner attribution must fence originals before their separate cleanup"
        )


ATTRIBUTION_SCENARIOS = [
    unrelated_purge_does_not_disable_owner,
    linked_person_change_advances_source_revision,
    attribution_mutation_rolls_back,
    link_reassignment_fences_both_originals,
    mention_role_fences_only_its_event,
    purge_is_idempotent_and_rolls_back,
    copy_rebase_fences_surviving_link,
    purged_link_stops_following_person_changes,
    attribution_write_waits_for_original_transaction,
    source_index_follows_commits_and_rollback,
    principal_erasure_fences_attribution_and_rolls_back,
]
