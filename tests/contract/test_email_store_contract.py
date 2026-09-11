"""Shared scoped email-store behavior on the deterministic and PostgreSQL tiers."""

from datetime import timedelta

import pytest

from agent_core.domain.email import EmailRecord
from agent_core.domain.errors import ConflictError
from agent_core.ports.email import EmailStore
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT, memory_uow_factory, principal


def record(key: str = "a", *, revision: int = 1) -> EmailRecord:
    return EmailRecord(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        kind="draft",
        key=key,
        revision=revision,
        payload={"body": "First draft", "nested": {"items": ["one"]}},
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=revision),
    )


async def assert_email_store_contract(store: EmailStore) -> None:
    """Every backend runs this same mutation, scoping, cursor and aliasing contract."""
    owner = principal()
    foreign_owner = owner.model_copy(update={"principal_id": "other"})
    foreign_tenant = owner.model_copy(update={"tenant_id": "other"})
    assert await store.get(owner, "draft", "a") is None
    first = record()
    stored = await store.put(first, expected_revision=0)
    first.payload["body"] = "Changed caller object"
    stored.payload["body"] = "Changed returned object"
    fetched = await store.get(owner, "draft", "a")
    assert fetched is not None
    assert fetched.payload["body"] == "First draft"
    nested = fetched.payload["nested"]
    assert isinstance(nested, dict)
    nested["items"].append("Changed")
    reread = await store.get(owner, "draft", "a")
    assert reread is not None
    assert reread.payload["nested"] == {"items": ["one"]}
    assert await store.get(foreign_owner, "draft", "a") is None
    assert await store.get(foreign_tenant, "draft", "a") is None
    assert await store.get(owner, "thread", "a") is None
    assert await store.list(foreign_owner, "draft") == []
    assert await store.list(foreign_tenant, "draft") == []
    for expected, revision in ((0, 1), (0, 2), (1, 3), (2, 3), (-1, 1)):
        with pytest.raises(ConflictError):
            await store.put(record(revision=revision), expected_revision=expected)
    changed = await store.put(record(revision=2), expected_revision=1)
    assert changed.revision == 2
    await store.put(record("c"), expected_revision=0)
    await store.put(record("b"), expected_revision=0)
    assert [item.key for item in await store.list(owner, "draft", limit=2)] == ["a", "b"]
    assert [item.key for item in await store.list(owner, "draft", after="b", limit=2)] == ["c"]
    listed = await store.list(owner, "draft", limit=1)
    listed[0].payload["body"] = "Changed list result"
    unchanged = await store.get(owner, "draft", "a")
    assert unchanged is not None
    assert unchanged.payload["body"] == "First draft"
    for boundary in (0, -1, 1001):
        with pytest.raises(ValueError):
            await store.list(owner, "draft", limit=boundary)
    for actor, expected in ((owner, 1), (foreign_owner, 2), (foreign_tenant, 2)):
        with pytest.raises(ConflictError):
            await store.delete(actor, "draft", "a", expected_revision=expected)
    await store.delete(owner, "draft", "a", expected_revision=2)
    assert await store.get(owner, "draft", "a") is None
    with pytest.raises(ConflictError):
        await store.delete(owner, "draft", "a", expected_revision=2)


async def test_email_store_is_available_and_passes_shared_contract() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        store = getattr(uow, "email", None)
        assert store is not None, "the ordinary unit of work must expose the email store"
        await assert_email_store_contract(store)


async def test_memory_email_lock_serializes_and_releases_after_error() -> None:
    import asyncio

    from agent_core.adapters.persistence.email import InMemoryEmailStore

    store = InMemoryEmailStore()
    entered = asyncio.Event()

    async def contender() -> None:
        async with store.lock(principal()):
            entered.set()

    class IntentionalFailureError(Exception):
        pass

    task = None
    try:
        with pytest.raises(IntentionalFailureError):
            async with store.lock(principal()):
                task = asyncio.create_task(contender())
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(entered.wait(), timeout=0.05)
                other = principal().model_copy(update={"principal_id": "other"})
                async with asyncio.timeout(1):
                    async with store.lock(other):
                        pass
                raise IntentionalFailureError
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        assert entered.is_set()
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
