"""Bulk mail never forms communication memory (ADR-0116).

The unsubscribe census and the assessment's ``bulk`` verdict are deterministic
signals that already exist; these tests pin that neither the formation service
nor the refresh lets a bulk message register a source, form a memory, or create
a provisional person.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from agent_core.domain.email import EmailRecord
from agent_core.domain.email_semantics import semantic_source_key
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import MemoryRecord
from agent_core.memory.email_people import EmailPeopleFormationService
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from tests.contract.memory_fixtures import browse_query, semantic_stack
from tests.contract.support import ids, principal


def _census_key(account_id: str, provider_thread_id: str) -> str:
    """The account-qualified thread key the census index and exclusions share."""
    return hashlib.sha256(f"{account_id}:{provider_thread_id}".encode()).hexdigest()


async def _index_thread_in_census(uow: RepositoryUnitOfWork, account_id: str, thread: str) -> None:
    stamp = datetime(2026, 9, 22, tzinfo=UTC)
    await uow.email.put(
        EmailRecord(
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            kind="subscription_thread",
            key=_census_key(account_id, thread),
            revision=1,
            payload={"subscription_id": "census-row"},
            created_at=stamp,
            updated_at=stamp,
        ),
        expected_revision=0,
    )


async def _stored_memories(factory: UnitOfWorkFactory) -> list[MemoryRecord]:
    async with factory() as uow:
        return await uow.memories.browse(browse_query())


async def test_a_census_indexed_thread_registers_no_source_and_forms_nothing() -> None:
    """Both semantic policies refuse a thread the unsubscribe census indexes."""
    factory, legacy, source, fact, _ = await semantic_stack()
    async with factory() as uow:
        await _index_thread_in_census(uow, source.account_id, source.provider_thread_id)
    people = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )

    with pytest.raises(ConflictError, match="bulk"):
        await people.register_source(source)
    with pytest.raises(ConflictError, match="bulk"):
        await people.form(source, [fact])
    with pytest.raises(ConflictError, match="bulk"):
        await legacy.form(source, [fact])

    assert await _stored_memories(factory) == []
    key = semantic_source_key(source.account_id, source.provider_thread_id, source.message_id)
    async with factory() as uow:
        assert await uow.email.get(principal(), "semantic_source", key) is None
        assert await uow.email.list(principal(), "semantic_source") == []


async def test_a_historical_import_skips_a_retained_source_the_census_now_indexes() -> None:
    """A retained passage from a sender later recognized as bulk is skipped, not analyzed."""
    factory, legacy, source, _fact, _ = await semantic_stack()
    await legacy.register_source(source)
    key = semantic_source_key(source.account_id, source.provider_thread_id, source.message_id)
    async with factory() as uow:
        retained = await uow.email.get(principal(), "semantic_source", key)
        assert retained is not None
        await _index_thread_in_census(uow, source.account_id, source.provider_thread_id)

    async def guard(_uow: RepositoryUnitOfWork) -> None:
        return None

    importer = EmailPeopleFormationService(
        factory,
        legacy._clock,
        ids(),
        principal(),
        provider="fake",
        model="scripted",
        import_window=(source.sent_at - timedelta(days=1), source.sent_at + timedelta(days=1)),
        import_guard=guard,
    )
    assert await importer.import_passage(retained, after_offset=None) is None
    assert await _stored_memories(factory) == []
