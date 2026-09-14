"""One contract reused by deterministic memory and real PostgreSQL adapters."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_core.adapters.persistence.calls import InMemoryCallStore, call_record_to_domain
from agent_core.adapters.persistence.sqlalchemy_models import CallRecordRow
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

    for key, payload in (
        ("due", {"active": True, "retry_at": (now - timedelta(seconds=1)).isoformat()}),
        ("future", {"active": True, "retry_at": (now + timedelta(hours=1)).isoformat()}),
        ("inactive", {"active": False, "retry_at": now.isoformat()}),
        ("missing", {"active": True}),
        ("null", {"active": True, "retry_at": None}),
        (
            "offset",
            {"active": True, "retry_at": now.astimezone(timezone(timedelta(hours=3))).isoformat()},
        ),
    ):
        await store.put(
            record.model_copy(update={"kind": "receipt", "key": key, "payload": payload}),
            expected_revision=0,
        )
    assert [r.key for r in await store.list(owner, "receipt", active=True)] == [
        "due",
        "future",
        "missing",
        "null",
        "offset",
    ]
    assert [r.key for r in await store.list(owner, "receipt", active=False)] == ["inactive"]
    assert [r.key for r in await store.list(owner, "receipt", active=True, due_at=now)] == [
        "due",
        "missing",
        "null",
        "offset",
    ]
    assert [
        r.key
        for r in await store.list(owner, "receipt", active=True, due_at=now, after="due", limit=2)
    ] == ["missing", "null"]
    for invalid in ("invalid", now.replace(tzinfo=None).isoformat(), 123):
        malformed = record.model_copy(update={"key": "malformed", "payload": {"retry_at": invalid}})
        with pytest.raises(ValueError, match="retry time"):
            await store.put(malformed, expected_revision=0)
        assert await store.get(owner, "call", "malformed") is None


async def test_memory_call_store_contract() -> None:
    await call_store_contract(InMemoryCallStore())


@pytest.mark.parametrize("retry_at", ["invalid", "2026-09-14T00:00:00", 123])
def test_call_record_rejects_malformed_retry_time_on_creation_and_hydration(
    retry_at: object,
) -> None:
    values = {
        "tenant_id": "t",
        "principal_id": "p",
        "kind": "receipt",
        "key": "receipt",
        "revision": 1,
        "payload": {"retry_at": retry_at},
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    with pytest.raises(ValueError, match="retry time"):
        CallRecord.model_validate(values)
    with pytest.raises(ValueError, match="retry time"):
        call_record_to_domain(CallRecordRow(**values))
