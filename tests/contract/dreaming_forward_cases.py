"""Forward-only admission preserves all original source and erasure checks."""

from datetime import timedelta
from uuid import UUID

from agent_core.domain.events import NewEvent
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Factory
from tests.contract.reconsolidation_source_cases import seed_sources
from tests.contract.support import NOW, SESSION_ID, principal


async def forward_only_sources(factory: Factory) -> None:
    async with factory() as uow:
        await seed_sources(uow)
        for i in range(1000, 1200):
            await uow.memories.upsert_belief(
                memory(belief_id=i).model_copy(
                    update={
                        "store_position": await uow.memories.next_position(),
                        "source_event_ids": [1],
                    }
                )
            )
    async with factory() as uow:
        event_id = await uow.events.dreaming_cutover_event_id()
        async with uow.people.lock(principal()):
            boundary = await uow.people.enable_forward_dreaming(principal(), event_id, NOW)
        assert boundary.creation_sequence == 202
        assert await uow.people.dreaming_attribution_ready(principal())
        assert (
            await uow.people.forward_dreaming_boundary(
                principal().model_copy(update={"principal_id": "someone-else"})
            )
            is None
        )
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=principal().principal_id,
                payload={"content": "I prefer concise answers."},
            )
        )
        # Old evidence, even copied into many new records, cannot crowd out new neighbors.
        for i in range(503, 540):
            await uow.memories.upsert_belief(
                memory(belief_id=i).model_copy(
                    update={
                        "store_position": await uow.memories.next_position(),
                        "source_event_ids": [1],
                    }
                )
            )
        for i in (540, 541):
            await uow.memories.upsert_belief(
                memory(belief_id=i).model_copy(
                    update={
                        "store_position": await uow.memories.next_position(),
                        "source_event_ids": [event.sequence],
                        "created_at": NOW - timedelta(days=365),
                    }
                )
            )
        await uow.memories.upsert_belief(
            memory(belief_id=542).model_copy(
                update={
                    "store_position": await uow.memories.next_position(),
                    "source_event_ids": [1, event.sequence],
                }
            )
        )
    async with factory() as uow:
        event_id = await uow.events.dreaming_cutover_event_id()
        async with uow.people.lock(principal()):
            assert (
                await uow.people.enable_forward_dreaming(
                    principal(), event_id, NOW + timedelta(days=1)
                )
                == boundary
            )
        job = await uow.reconsolidation.claim_due(principal(), NOW, "forward-test")
        assert job is not None
        page = await uow.reconsolidation.inventory(principal(), job.lease_token, NOW)
        assert {s.belief_id for s in page.sources} == {UUID(int=540), UUID(int=541)}
        neighbors = await uow.reconsolidation.neighbors(principal(), page.sources[0], NOW)
        assert {s.belief_id for s in neighbors} == {UUID(int=540), UUID(int=541)}
        await uow.reconsolidation.checkpoint(principal(), job.lease_token, page, (neighbors,), NOW)
        group = await uow.reconsolidation.claim_group(principal(), job.lease_token, NOW)
        assert group is not None
        async with uow.people.lock(principal()):
            assert (
                await uow.reconsolidation.original_input(
                    principal(), job.lease_token, group.id, NOW
                )
                is not None
            )
        await uow.memories.fence_for_erasure(principal(), [UUID(int=540)])
        async with uow.people.lock(principal()):
            assert (
                await uow.reconsolidation.original_input(
                    principal(), job.lease_token, group.id, NOW
                )
                is None
            )
