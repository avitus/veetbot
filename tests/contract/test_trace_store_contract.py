"""Faithful recall-trace storage contract."""

from agent_core.adapters.memory.in_memory import InMemoryTraceStore
from tests.contract.memory_fixtures import trace
from tests.contract.support import RUN_ID, principal


async def test_trace_store_round_trips_the_same_record() -> None:
    store = InMemoryTraceStore()
    value = trace()
    await store.record(value)
    assert await store.get(value.id, principal()) == value
    assert await store.for_turn(RUN_ID) == [value]
