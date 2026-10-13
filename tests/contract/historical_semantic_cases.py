"""Historical dense recall obeys the existing two temporal coordinates."""

from collections.abc import Callable
from datetime import timedelta

from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.semantic_memory_cases import SPACE, VECTOR
from tests.contract.support import NOW, principal


async def historical_revision_is_not_current_evidence(
    factory: UnitOfWorkFactory, advance: Callable[[timedelta], None]
) -> None:
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory(statement="User avoids dairy"))
        captured = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert captured is not None
        assert await uow.semantic_memory.put(principal(), captured, VECTOR)
    advance(timedelta(days=1))
    async with factory() as uow:
        current = original.model_copy(
            update={"statement": "User now eats dairy", "store_position": 2}
        )
        await uow.memories.reinforce(current)
        historical = await uow.semantic_memory.capture_revision(principal(), captured.source, SPACE)
        assert historical is not None, "A governed historical original needs its own fenced vector"
        assert historical.record.statement == original.statement
        assert await uow.semantic_memory.put(principal(), historical, VECTOR)
        latest = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert latest is not None
        assert await uow.semantic_memory.put(principal(), latest, (0.0, 1.0, 0.0))
    async with factory() as uow:
        then = NOW + timedelta(hours=1)
        query = recall_query(text="breakfast suggestions").model_copy(
            update={"as_of": then, "known_at": then}
        )
        hits = await uow.semantic_memory.search(query, SPACE, VECTOR, limit=1)
        assert [hit.record.statement for hit in hits] == [original.statement]
        assert hits[0].similarity == 1
        current_hits = await uow.semantic_memory.search(
            recall_query(text="breakfast suggestions"), SPACE, VECTOR, limit=1
        )
        assert [hit.record.statement for hit in current_hits] == [current.statement]
        assert current_hits[0].similarity == 0


async def historical_visibility_and_stale_writes(
    factory: UnitOfWorkFactory, advance: Callable[[timedelta], None]
) -> None:
    from agent_core.domain.memory import Portability, Sensitivity

    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory(statement="User avoids dairy"))
        initial = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert initial is not None
    then = NOW + timedelta(hours=1)
    query = recall_query(text="breakfast suggestions").model_copy(
        update={"as_of": then, "known_at": then}
    )
    advance(timedelta(days=1))
    async with factory() as uow:
        current = original.model_copy(
            update={"statement": "User now eats dairy", "store_position": 2}
        )
        await uow.memories.reinforce(current)
        old = await uow.semantic_memory.capture_revision(principal(), initial.source, SPACE)
        assert old is not None and await uow.semantic_memory.put(principal(), old, VECTOR)
        assert await uow.semantic_memory.search(query, SPACE, VECTOR, limit=1)
        for changed in (
            {"principal_id": "foreign"},
            {"tenant_id": "foreign"},
            {"exclude_ids": (original.id,)},
            {"include_ids": ()},
        ):
            assert (
                await uow.semantic_memory.search(
                    query.model_copy(update=changed), SPACE, VECTOR, limit=1
                )
                == []
            )
        current = current.model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
        await uow.memories.reinforce(current)
        assert not await uow.semantic_memory.put(principal(), old, VECTOR)
        assert await uow.semantic_memory.search(query, SPACE, VECTOR, limit=1) == []
        restricted = await uow.semantic_memory.capture_revision(principal(), initial.source, SPACE)
        assert restricted is not None and restricted.record.sensitivity is Sensitivity.RESTRICTED
        assert await uow.semantic_memory.put(principal(), restricted, VECTOR)
        assert (
            await uow.semantic_memory.search(
                query.model_copy(update={"sensitivity_ceiling": Sensitivity.INTERNAL}),
                SPACE,
                VECTOR,
                limit=1,
            )
            == []
        )
        current = current.model_copy(
            update={
                "sensitivity": Sensitivity.INTERNAL,
                "scope": "private-project",
                "portability": Portability.LOCAL,
            }
        )
        await uow.memories.reinforce(current)
        local = await uow.semantic_memory.capture_revision(principal(), initial.source, SPACE)
        assert local is not None and await uow.semantic_memory.put(principal(), local, VECTOR)
        assert (
            await uow.semantic_memory.search(
                query.model_copy(update={"subjects": []}), SPACE, VECTOR, limit=1
            )
            == []
        )
        await uow.memories.fence_for_erasure(principal(), [original.id])
        assert not await uow.semantic_memory.put(principal(), local, VECTOR)
        assert (
            await uow.semantic_memory.capture_revision(principal(), initial.source, SPACE) is None
        )
        assert await uow.semantic_memory.search(query, SPACE, VECTOR, limit=1) == []


async def historical_selection_precedes_similarity(
    factory: UnitOfWorkFactory, advance: Callable[[timedelta], None]
) -> None:
    # Two revisions at exactly the same knowledge time: the latter wins before
    # similarity ordering, even when an older revision has a stronger vector.
    async with factory() as uow:
        original = await uow.memories.upsert_belief(memory(statement="User avoids dairy"))
        before = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert before is not None
        current = original.model_copy(
            update={"statement": "User now eats dairy", "store_position": 2}
        )
        await uow.memories.reinforce(current)
        latest = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert latest is not None
        for source, vector in ((before, VECTOR), (latest, (0.0, 1.0, 0.0))):
            historical = await uow.semantic_memory.capture_revision(
                principal(), source.source, SPACE
            )
            assert historical is not None and await uow.semantic_memory.put(
                principal(), historical, vector
            )
        query = recall_query(text="breakfast suggestions").model_copy(
            update={"known_at": NOW, "as_of": NOW}
        )
        hits = await uow.semantic_memory.search(query, SPACE, VECTOR, limit=1)
        assert [hit.record.statement for hit in hits] == [current.statement]
        assert hits[0].similarity == 0
        bounds = await uow.semantic_memory.bounds(principal(), include_history=True)
        page = await uow.semantic_memory.inventory(
            principal(), after=0, through=bounds.creation, limit=1, include_history=True
        )
        assert page.cursor == 1 and page.sources == (before.source,)
        second = await uow.semantic_memory.inventory(
            principal(), after=1, through=bounds.creation, limit=1, include_history=True
        )
        assert second.cursor == 2 and second.sources == (latest.source,)


async def historical_revision_restrictions_precede_limit(
    factory: UnitOfWorkFactory, advance: Callable[[timedelta], None]
) -> None:
    from agent_core.domain.memory import BeliefType, Portability, Sensitivity

    variants: tuple[dict[str, object], ...] = (
        {},
        {"sensitivity": Sensitivity.RESTRICTED},
        {"portability": Portability.LOCAL, "scope": "other-project"},
        {"belief_type": BeliefType.FACT},
    )
    records = []
    async with factory() as uow:
        for index, variant in enumerate(variants):
            record = memory(
                belief_id=700 + index, statement=f"Older meal constraint {index}"
            ).model_copy(update={"store_position": index + 1, **variant})
            records.append(await uow.memories.upsert_belief(record))
        bounds = await uow.semantic_memory.bounds(principal())
        page = await uow.semantic_memory.inventory(principal(), after=0, through=bounds.creation)
        originals = {source.belief_id: source for source in page.sources}
    advance(timedelta(days=1))
    async with factory() as uow:
        for index, record in enumerate(records):
            await uow.memories.reinforce(
                record.model_copy(
                    update={
                        "statement": f"Updated meal constraint {index}",
                        "store_position": index + 10,
                        "sensitivity": Sensitivity.INTERNAL,
                        "portability": Portability.PORTABLE,
                        "scope": "project-a",
                        "belief_type": BeliefType.PREFERENCE,
                    }
                )
            )
            historical = await uow.semantic_memory.capture_revision(
                principal(), originals[record.id], SPACE
            )
            assert historical is not None
            assert await uow.semantic_memory.put(
                principal(), historical, (0.8, 0.6, 0.0) if index == 0 else VECTOR
            )
            assert await uow.semantic_memory.indexed(principal(), historical)
        query = recall_query(
            text="meal",
            sensitivity_ceiling=Sensitivity.INTERNAL,
            belief_types=[BeliefType.PREFERENCE],
        ).model_copy(update={"known_at": NOW, "as_of": NOW})
        hits = await uow.semantic_memory.search(query, SPACE, VECTOR, limit=1)
        assert [hit.record.id for hit in hits] == [records[0].id], (
            "Historical sensitivity, scope and type restrictions must precede the dense limit"
        )
