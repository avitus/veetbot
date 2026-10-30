"""Shared sealed-store isolation, tombstone and rollback contract."""

from dataclasses import replace
from uuid import uuid4

from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.integration_secrets import IntegrationSecret
from agent_core.ports.integration_secrets import IntegrationSecretStore
from tests.contract.support import NOW
from tests.integration.m2_support import memory_settings


async def store_contract(store: IntegrationSecretStore, owner: Principal) -> None:
    record = IntegrationSecret(
        owner.tenant_id,
        owner.principal_id,
        uuid4(),
        "Example",
        "https://api.example.com",
        "bearer",
        1,
        NOW,
        NOW,
        "ciphertext",
    )
    async with store.locked(owner):
        await store.put(owner, record)
    assert await store.get(owner, record.id) == record
    assert await store.list(owner) == [record]
    for other in (
        owner.model_copy(update={"principal_id": "other"}),
        owner.model_copy(update={"tenant_id": "other"}),
    ):
        assert await store.get(other, record.id) is None
        assert await store.list(other) == []
    async with store.locked(owner):
        await store.put(owner, replace(record, revision=2, sealed_value=None, deleted_at=NOW))
    assert await store.list(owner) == []
    tombstone = await store.get(owner, record.id)
    assert tombstone is not None and tombstone.sealed_value is None


async def test_memory_store_contract() -> None:
    async with (
        build(settings=memory_settings(), storage="memory") as composition,
        composition.uow_factory() as uow,
    ):
        store = getattr(uow, "integration_secrets", None)
        assert store is not None, "The sealed integration-key repository is not composed."
        await store_contract(store, composition.principal)
