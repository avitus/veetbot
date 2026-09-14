"""One contract reused by deterministic memory and real PostgreSQL adapters."""

from datetime import UTC, datetime

import pytest

from agent_core.adapters.persistence.calls import InMemoryCallStore
from agent_core.domain.agents import Principal
from agent_core.domain.calls import CallRecord
from agent_core.domain.errors import ConflictError
from agent_core.ports.calls import CallStore


async def call_store_contract(store: CallStore) -> None:
    owner = Principal(tenant_id="call-tenant", principal_id="owner", roles=set(), scopes=set())
    foreign = owner.model_copy(update={"tenant_id": "other-tenant"})
    other = owner.model_copy(update={"principal_id": "other-owner"})
    now = datetime.now(UTC)
    record = CallRecord(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        kind="call",
        key="call-1",
        revision=1,
        payload={"summary": "Private"},
        created_at=now,
        updated_at=now,
    )
    assert await store.put(record, expected_revision=0) == record
    assert await store.get(owner, "call", "call-1") == record
    assert await store.get(foreign, "call", "call-1") is None
    assert await store.get(other, "call", "call-1") is None
    assert await store.list(foreign, "call") == []
    with pytest.raises(ConflictError):
        await store.put(record, expected_revision=0)
    erased = record.model_copy(update={"revision": 2, "payload": {"erased": True}})
    async with store.lock(owner):
        await store.put(erased, expected_revision=1)
    assert (await store.list(owner, "call"))[0].payload == {"erased": True}
    assert await store.list(owner, "call", after="call-1") == []
    with pytest.raises(ConflictError):
        await store.put(record.model_copy(update={"revision": 2}), expected_revision=1)
    with pytest.raises(ValueError):
        await store.list(owner, "call", limit=0)


async def test_memory_call_store_contract() -> None:
    await call_store_contract(InMemoryCallStore())
