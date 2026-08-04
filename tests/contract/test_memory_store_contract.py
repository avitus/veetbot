"""Structured belief-store contract."""

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.memory.in_memory import InMemoryMemoryStore
from agent_core.domain.errors import NotFoundError
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.support import NOW, principal


async def test_memory_store_enforces_scope_and_lifecycle() -> None:
    store = InMemoryMemoryStore(FixedClock(NOW))
    value = memory()
    await store.upsert_belief(value)
    assert await store.query(recall_query()) == [value]
    assert await store.query(recall_query(tenant_id="tenant-b")) == []
    with pytest.raises(NotFoundError):
        await store.get(value.id, principal().model_copy(update={"principal_id": "other"}))
