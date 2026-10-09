"""Historical summary reconstruction and source-complete identity boundaries."""

from datetime import datetime, timedelta
from uuid import UUID

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.domain.memory import Portability, Sensitivity
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.retrieval import HybridMemoryRetriever
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.reconsolidation_attribution_cases import person
from tests.contract.reconsolidation_summary_recall_cases import summary_query, summary_recall_inputs
from tests.contract.support import NOW, SESSION_ID, principal


def retriever(factory: UnitOfWorkFactory, clock: FixedClock) -> HybridMemoryRetriever:
    return HybridMemoryRetriever(
        factory, clock, RandomIdFactory(), principal(), reconsolidation_enabled=True
    )


async def historical_summary_correction(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    operation, originals = await summary_recall_inputs(factory, omit_last=True)
    query = summary_query().model_copy(update={"as_of": NOW, "known_at": NOW})
    reader = retriever(factory, clock)
    initial = await reader.recall(query, session_id=SESSION_ID)
    assert operation.id in {item.belief_id for item in initial.items}
    clock.advance(timedelta(seconds=20))
    async with factory() as uow:
        await uow.memories.reinforce(
            originals[-1].model_copy(
                update={
                    "statement": "User corrected the omitted option",
                    "updated_at": clock.now(),
                    "store_position": await uow.memories.next_position(),
                }
            )
        )
        terminal = await uow.reconsolidation.summary_operation(principal(), operation.id)
        assert terminal is not None and terminal.state == "invalidated"
        head = await uow.memories.head_position(principal())
    historical = await reader.recall(query, session_id=SESSION_ID, turn_id=UUID(int=991))
    assert historical.items == initial.items, "reconstruct exact clauses after projection purge"
    service = GovernedMemoryService(factory, clock, RandomIdFactory(), principal())
    assert (await service.get_recall_trace(historical.trace_id)).rendered == historical.rendered
    async with factory() as uow:
        assert operation.id in {
            item.belief_id
            for item in (await uow.traces.user_view(UUID(int=991), "private", "restricted")).beliefs
        }
        assert await uow.reconsolidation.summary_operation(principal(), operation.id) == terminal
        assert await uow.memories.head_position(principal()) == head
    for changes in (
        {"known_at": clock.now()},
        {"known_at": None},
        {"as_of": clock.now(), "known_at": clock.now()},
    ):
        result = await reader.recall(query.model_copy(update=changes), session_id=SESSION_ID)
        assert operation.id not in {item.belief_id for item in result.items}


async def historical_summary_current_privacy(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    operation, originals = await summary_recall_inputs(factory, omit_last=True)
    query = summary_query().model_copy(update={"as_of": NOW, "known_at": NOW})
    reader = retriever(factory, clock)
    result = await reader.recall(query, session_id=SESSION_ID, turn_id=UUID(int=991))
    assert operation.id in {item.belief_id for item in result.items}
    clock.advance(timedelta(seconds=20))
    for changes in (
        {"sensitivity": Sensitivity.RESTRICTED},
        {"portability": Portability.LOCAL, "scope": "another-project"},
    ):
        async with factory() as uow:
            await uow.memories.reinforce(
                originals[-1].model_copy(
                    update={
                        **changes,
                        "updated_at": clock.now(),
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
        hidden = await reader.recall(query, session_id=SESSION_ID)
        assert operation.id not in {item.belief_id for item in hidden.items}
        service = GovernedMemoryService(factory, clock, RandomIdFactory(), principal())
        assert not (await service.get_recall_trace(result.trace_id)).rendered
    async with factory() as uow:
        await uow.memories.fence_for_erasure(principal(), [originals[-1].id])
    hidden = await reader.recall(query, session_id=SESSION_ID)
    assert operation.id not in {item.belief_id for item in hidden.items}


async def historical_summary_expiry_and_boundaries(
    factory: UnitOfWorkFactory, clock: FixedClock
) -> None:
    operation, _ = await summary_recall_inputs(factory, expires_at=NOW + timedelta(seconds=30))
    clock.advance(timedelta(seconds=40))
    reader = retriever(factory, clock)
    for as_of, known_at, present in (
        (NOW, NOW, True),
        (NOW, None, True),
        (None, NOW, False),
        (NOW - timedelta(microseconds=1), NOW, False),
        (NOW, NOW - timedelta(microseconds=1), False),
        (NOW + timedelta(seconds=30), NOW, False),
    ):
        result = await reader.recall(
            summary_query().model_copy(update={"as_of": as_of, "known_at": known_at}),
            session_id=SESSION_ID,
        )
        assert (operation.id in {item.belief_id for item in result.items}) is present


async def historical_summary_trace_clock(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    operation, _ = await summary_recall_inputs(factory, expires_at=NOW + timedelta(seconds=30))
    reader = retriever(factory, clock)
    result = await reader.recall(
        summary_query().model_copy(update={"known_at": NOW}),
        session_id=SESSION_ID,
        turn_id=UUID(int=991),
    )
    assert operation.id in {item.belief_id for item in result.items}
    clock.advance(timedelta(seconds=40))
    service = GovernedMemoryService(factory, clock, RandomIdFactory(), principal())
    assert (await service.get_recall_trace(result.trace_id)).rendered == result.rendered, (
        "known-at-only inspection preserves the original recall's effective instant"
    )
    async with factory() as uow:
        view = await uow.traces.user_view(UUID(int=991), "private", "restricted")
        assert operation.id in {item.belief_id for item in view.beliefs}


async def historical_summary_effective_instant(
    factory: UnitOfWorkFactory, clock: FixedClock
) -> None:
    class AdvancingClock(FixedClock):
        calls = 0

        def now(self) -> datetime:
            self.calls += 1
            return NOW if self.calls == 1 else NOW + timedelta(seconds=40)

    operation, _ = await summary_recall_inputs(factory, expires_at=NOW + timedelta(seconds=30))
    reader = HybridMemoryRetriever(
        factory, AdvancingClock(NOW), RandomIdFactory(), principal(), reconsolidation_enabled=True
    )
    result = await reader.recall(
        summary_query().model_copy(update={"known_at": NOW}),
        session_id=SESSION_ID,
        turn_id=UUID(int=991),
    )
    assert operation.id in {item.belief_id for item in result.items}
    service = GovernedMemoryService(factory, clock, RandomIdFactory(), principal())
    assert (await service.get_recall_trace(result.trace_id)).rendered == result.rendered, (
        "trace creation after expiry must not change the historical query's effective instant"
    )
    async with factory() as uow:
        view = await uow.traces.user_view(UUID(int=991), "private", "restricted")
        assert operation.id in {item.belief_id for item in view.beliefs}


async def historical_summary_erasure_and_versions(
    factory: UnitOfWorkFactory, clock: FixedClock
) -> None:
    operation, originals = await summary_recall_inputs(factory, omit_last=True)
    clock.advance(timedelta(seconds=20))
    async with factory() as uow:
        for statement in ("User corrected the omitted option", originals[-1].statement):
            await uow.memories.reinforce(
                originals[-1].model_copy(
                    update={
                        "statement": statement,
                        "updated_at": clock.now(),
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
    reader = retriever(factory, clock)
    query = summary_query().model_copy(update={"as_of": NOW, "known_at": NOW})
    result = await reader.recall(query, session_id=SESSION_ID, turn_id=UUID(int=991))
    assert operation.id in {item.belief_id for item in result.items}
    latest = await reader.recall(
        query.model_copy(update={"known_at": clock.now()}), session_id=SESSION_ID
    )
    assert operation.id not in {item.belief_id for item in latest.items}, (
        "equal text cannot certify a different source revision"
    )
    async with factory() as uow:
        await uow.memories.fence_for_erasure(principal(), [originals[-1].id])
    hidden = await reader.recall(query, session_id=SESSION_ID)
    assert operation.id not in {item.belief_id for item in hidden.items}
    service = GovernedMemoryService(factory, clock, RandomIdFactory(), principal())
    assert not (await service.get_recall_trace(result.trace_id)).rendered
    async with factory() as uow:
        view = await uow.traces.user_view(UUID(int=991), "private", "restricted")
        assert operation.id not in {item.belief_id for item in view.beliefs}


async def summary_people_intersection(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    operation, _ = await summary_recall_inputs(factory, omit_last=True, people=(801, 802))
    reader = retriever(factory, clock)
    for people, present in (
        ((person().id,), False),
        ((person(802).id,), False),
        ((person().id, person(802).id), True),
    ):
        result = await reader.recall(
            summary_query().model_copy(update={"people_scope": people}), session_id=SESSION_ID
        )
        assert (operation.id in {item.belief_id for item in result.items}) is present, (
            "all support must satisfy the focal scope, even omitted support"
        )


async def historical_summary_people(factory: UnitOfWorkFactory, clock: FixedClock) -> None:
    operation, _ = await summary_recall_inputs(factory, people=(801, 801))
    reader = retriever(factory, clock)
    query = summary_query().model_copy(
        update={"as_of": NOW, "known_at": NOW, "people_scope": (person().id,)}
    )
    assert operation.id in {
        item.belief_id for item in (await reader.recall(query, session_id=SESSION_ID)).items
    }
    clock.advance(timedelta(seconds=20))
    async with factory() as uow, uow.people.lock(principal()):
        await uow.people.put(person(802), expected_revision=0)
        for index in range(2):
            current = await uow.people.get(
                principal(), UUID(int=811 + index), ceiling=Sensitivity.INTERNAL
            )
            assert current is not None
            await uow.people.put(
                current.model_copy(
                    update={"person_id": person(802).id, "revision": 2, "updated_at": clock.now()}
                ),
                expected_revision=1,
            )
    result = await reader.recall(query, session_id=SESSION_ID)
    assert operation.id in {item.belief_id for item in result.items}
    for changes in ({"known_at": clock.now()}, {"people_scope": (person(802).id,)}):
        result = await reader.recall(query.model_copy(update=changes), session_id=SESSION_ID)
        assert operation.id not in {item.belief_id for item in result.items}
    async with factory() as uow, uow.people.lock(principal()):
        await uow.people.fence_for_erasure(principal(), [person().id])
    assert operation.id not in {
        item.belief_id for item in (await reader.recall(query, session_id=SESSION_ID)).items
    }


SUMMARY_HISTORY_SCENARIOS = [
    historical_summary_correction,
    historical_summary_current_privacy,
    historical_summary_expiry_and_boundaries,
    historical_summary_trace_clock,
    historical_summary_effective_instant,
    historical_summary_erasure_and_versions,
    summary_people_intersection,
    historical_summary_people,
]
