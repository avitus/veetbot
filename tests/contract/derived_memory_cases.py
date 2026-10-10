"""Derived memory owner controls: one contract for both complete compositions."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock, RandomIdFactory
from agent_core.application.public_services import PublicMemoryService
from agent_core.domain.derived_memory import DerivedMemoryView
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import (
    BeliefType,
    MemoryReviewOutcome,
    MemoryStatus,
    Portability,
    Sensitivity,
)
from agent_core.domain.reconsolidation_summary import SummaryClause
from agent_core.domain.views import MemoryView
from agent_core.memory.formation import GovernedMemoryService
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.reconsolidation_source_cases import seed_sources
from tests.contract.reconsolidation_summary_cases import committed_summary
from tests.contract.reconsolidation_surface_cases import owner
from tests.contract.support import NOW, SESSION_ID


def service(factory: UnitOfWorkFactory, *, clock: FixedClock | None = None) -> PublicMemoryService:
    clock = clock or FixedClock(NOW)
    return PublicMemoryService(
        uow_factory=factory,
        clock=clock,
        derived_enabled=True,
        memory_for=lambda principal: GovernedMemoryService(
            factory, clock, RandomIdFactory(), principal
        ),
    )


async def browse_defaults_filters_and_keyset(factory: UnitOfWorkFactory) -> None:
    operation = await committed_summary(cast(Factory, factory), omitted=True)
    api = service(factory)
    kwargs: dict[str, Any] = {
        "ceiling": Sensitivity.INTERNAL,
        "statuses": None,
        "belief_types": None,
        "subject": None,
        "session_id": None,
        "text": None,
        "limit": 1,
        "cursor": None,
    }
    defaults = await api.list(owner(), **kwargs)
    assert all(isinstance(row, MemoryView) for row in defaults.items)
    first = await api.list(owner(), **kwargs, include_derived=True)
    assert isinstance(first.items[0], DerivedMemoryView) and first.items[0].id == operation.id
    assert first.next_cursor is not None
    pages = [first]
    while pages[-1].next_cursor is not None:
        pages.append(
            await api.list(
                owner(), **(kwargs | {"cursor": pages[-1].next_cursor}), include_derived=True
            )
        )
    ids = [row.id for page in pages for row in page.items]
    assert len(ids) == len(set(ids)) == 3 and set(ids) == {*operation.plan.member_ids, operation.id}
    for filters, expected in (
        ({"text": "concise unrelated-term"}, True),
        ({"text": "execution"}, False),  # Omitted source text must not match derived output.
        ({"text": ""}, True),
        ({"text": "   "}, True),
        ({"text": "!!!"}, False),
        ({"subject": "RELATED MEMORIES"}, True),
        ({"subject": "unrelated"}, False),
        ({"session_id": SESSION_ID}, True),
        ({"session_id": UUID(int=999)}, False),
        ({"belief_types": [BeliefType.PREFERENCE]}, True),
        ({"belief_types": [BeliefType.RELATIONSHIP]}, False),
        ({"statuses": [MemoryStatus.RETIRED]}, False),
        ({"flagged": True}, True),
        ({"flagged": False}, False),
        ({"ceiling": Sensitivity.PUBLIC}, False),
    ):
        page = await api.list(owner(), **(kwargs | {"limit": 200} | filters), include_derived=True)
        assert (operation.id in {row.id for row in page.items}) is expected, filters
    for foreign in (
        owner().model_copy(update={"tenant_id": "foreign"}),
        owner().model_copy(update={"principal_id": "foreign"}),
    ):
        assert (await api.list(foreign, **kwargs, include_derived=True)).items == []
        with pytest.raises(NotFoundError):
            await api.get(foreign, operation.id, ceiling=Sensitivity.RESTRICTED)


async def review_preserves_evidence_and_historical_scope(factory: UnitOfWorkFactory) -> None:
    operation = await committed_summary(cast(Factory, factory))
    api = service(factory)
    async with factory() as uow:
        originals = [await uow.memories.get(key, owner()) for key in operation.plan.member_ids]
    initial = await api.get(owner(), operation.id, ceiling=Sensitivity.INTERNAL)
    assert isinstance(initial, DerivedMemoryView) and initial.content is not None
    dismissed = await api.review(
        owner(),
        operation.id,
        MemoryReviewOutcome.DISMISS,
        ceiling=Sensitivity.INTERNAL,
        key="dismiss",
    )
    assert isinstance(dismissed, DerivedMemoryView)
    assert dismissed.revision == 2 and dismissed.flagged_for_review is False
    assert dismissed.content == initial.content
    assert (
        await api.review(
            owner(),
            operation.id,
            MemoryReviewOutcome.DISMISS,
            ceiling=Sensitivity.INTERNAL,
            key="dismiss",
        )
        == dismissed
    )
    localized = await api.review(
        owner(),
        operation.id,
        MemoryReviewOutcome.NOT_HERE,
        ceiling=Sensitivity.INTERNAL,
        key="local",
    )
    assert isinstance(localized, DerivedMemoryView) and localized.content is not None
    assert localized.content.portability == Portability.LOCAL and localized.flagged_for_review
    assert (
        localized.content.model_copy(update={"portability": initial.content.portability})
        == initial.content
    )
    async with factory() as uow:
        for historical in ({}, {"as_of": NOW, "known_at": NOW}):
            assert (
                await uow.reconsolidation.get_summary(
                    owner(),
                    operation.id,
                    NOW,
                    ceiling=Sensitivity.INTERNAL,
                    current_scope="other",
                    **historical,
                )
                is None
            )
            assert (
                await uow.reconsolidation.get_summary(
                    owner(),
                    operation.id,
                    NOW,
                    ceiling=Sensitivity.INTERNAL,
                    current_scope=initial.content.scope,
                    **historical,
                )
                is not None
            )
        assert await uow.reconsolidation.update_summary_usage(
            owner(), operation.id, 0.2, NOW, cited=True
        )
        assert [await uow.memories.get(row.id, owner()) for row in originals] == list(originals)
    after_usage = await api.get(owner(), operation.id, ceiling=Sensitivity.INTERNAL)
    assert after_usage == localized, (
        "usage must preserve the immutable prepared payload and owner restriction"
    )


async def rejection_delete_and_erased_replays(factory: UnitOfWorkFactory) -> None:
    operation = await committed_summary(cast(Factory, factory), omitted=True)
    api = service(factory)
    async with factory() as uow:
        originals = [await uow.memories.get(key, owner()) for key in operation.plan.member_ids]
    rejected = await api.review(
        owner(),
        operation.id,
        MemoryReviewOutcome.UNTRUE,
        ceiling=Sensitivity.INTERNAL,
        key="untrue",
    )
    assert isinstance(rejected, DerivedMemoryView)
    assert rejected.status == "retired" and rejected.content is None and rejected.sources == ()
    assert (
        await api.review(
            owner(),
            operation.id,
            MemoryReviewOutcome.UNTRUE,
            ceiling=Sensitivity.INTERNAL,
            key="untrue",
        )
        == rejected
    )
    async with factory() as uow:
        for clocks in (
            {},
            {"as_of": NOW - timedelta(microseconds=1)},
            {"as_of": NOW, "known_at": NOW},
        ):
            assert (
                await uow.reconsolidation.get_summary(
                    owner(),
                    operation.id,
                    NOW,
                    ceiling=Sensitivity.RESTRICTED,
                    current_scope="user",
                    **clocks,
                )
                is None
            )
        assert [await uow.memories.get(row.id, owner()) for row in originals] == list(originals)
        assert (
            await uow.memories.outstanding_rejections(owner().tenant_id, owner().principal_id) == []
        ), "summary rejection must not reject an original atom"
    await api.delete(owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="delete")
    with pytest.raises(NotFoundError):
        await api.get(owner(), operation.id, ceiling=Sensitivity.RESTRICTED)
    async with factory() as uow:
        await uow.memories.fence_for_erasure(owner(), [originals[-1].id])
    await api.delete(owner(), operation.id, ceiling=Sensitivity.PUBLIC, key="delete")
    with pytest.raises(NotFoundError):
        await api.review(
            owner(),
            operation.id,
            MemoryReviewOutcome.UNTRUE,
            ceiling=Sensitivity.RESTRICTED,
            key="untrue",
        )
    with pytest.raises(ConflictError):
        await api.review(
            owner(),
            operation.id,
            MemoryReviewOutcome.DISMISS,
            ceiling=Sensitivity.RESTRICTED,
            key="delete",
        )
    with pytest.raises(NotFoundError):
        await api.delete(
            owner().model_copy(update={"principal_id": "foreign"}),
            operation.id,
            ceiling=Sensitivity.RESTRICTED,
            key="delete",
        )


async def summary_write_and_receipt_rollback(factory: UnitOfWorkFactory) -> None:
    operation = await committed_summary(cast(Factory, factory))
    api = service(factory)
    initial = await api.get(owner(), operation.id, ceiling=Sensitivity.INTERNAL)

    @asynccontextmanager
    async def failing() -> AsyncIterator[RepositoryUnitOfWork]:
        async with factory() as uow:
            yield uow
            raise RuntimeError("abort after receipt")

    with pytest.raises(RuntimeError, match="abort after receipt"):
        await service(cast(UnitOfWorkFactory, failing)).delete(
            owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="rollback"
        )
    assert await api.get(owner(), operation.id, ceiling=Sensitivity.INTERNAL) == initial
    async with factory() as uow:
        derivation, _ = api._write_key(
            owner(), "rollback", {"op": "delete", "memory_id": str(operation.id)}
        )
        assert await uow.reconsolidation.summary_write_receipt(owner(), derivation) is None
    await api.delete(owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="rollback")
    await api.delete(owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="rollback")


async def simultaneous_summary_retries(factory: UnitOfWorkFactory) -> None:
    operation = await committed_summary(cast(Factory, factory))
    api = service(factory)
    results = await asyncio.gather(
        *(
            api.review(
                owner(),
                operation.id,
                MemoryReviewOutcome.DISMISS,
                ceiling=Sensitivity.INTERNAL,
                key="same",
            )
            for _ in range(4)
        )
    )
    assert isinstance(results[0], DerivedMemoryView)
    assert all(result == results[0] for result in results) and results[0].revision == 2
    await asyncio.gather(
        *(
            api.delete(owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="delete")
            for _ in range(4)
        )
    )
    async with factory() as uow:
        stored = await uow.reconsolidation.summary_operation(owner(), operation.id)
        assert stored is not None and stored.revision == 3


async def summary_and_original_write_keys_conflict(factory: UnitOfWorkFactory) -> None:
    operation = await committed_summary(cast(Factory, factory))
    api = service(factory)
    await api.review(
        owner(),
        operation.id,
        MemoryReviewOutcome.DISMISS,
        ceiling=Sensitivity.INTERNAL,
        key="derived",
    )
    with pytest.raises(ConflictError):
        await api.delete(
            owner(), operation.plan.member_ids[0], ceiling=Sensitivity.INTERNAL, key="derived"
        )
    await api.review(
        owner(),
        operation.plan.member_ids[0],
        MemoryReviewOutcome.DISMISS,
        ceiling=Sensitivity.INTERNAL,
        key="original",
    )
    with pytest.raises(ConflictError):
        await api.delete(owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="original")


async def deletion_blocks_new_copies_and_prepared_commits(
    factory: UnitOfWorkFactory, *, committed_copy: bool = False
) -> None:
    async with factory() as uow:
        await seed_sources(cast(Stores, uow))
        second = await uow.memories.get(UUID(int=502), owner())
        await uow.memories.reinforce(
            second.model_copy(update={"statement": "I prefer local execution."})
        )
        originals = [await uow.memories.get(UUID(int=key), owner()) for key in (501, 502)]
        for index, original in enumerate(originals):
            await uow.memories.upsert_belief(
                original.model_copy(
                    update={
                        "id": UUID(int=600 + index),
                        "subject": "renamed subject",
                        "statement": original.statement.swapcase(),
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
        job = await uow.reconsolidation.claim_due(owner(), NOW, "test")
        assert job is not None
        page = await uow.reconsolidation.inventory(owner(), job.lease_token, NOW)
        await uow.reconsolidation.checkpoint(
            owner(), job.lease_token, page, (page.sources[:2], page.sources[2:]), NOW
        )
        groups = [
            await uow.reconsolidation.claim_group(owner(), job.lease_token, NOW) for _ in range(2)
        ]
        group = next(
            item for item in groups if item is not None and item.sources[0].belief_id.int == 501
        )
        copy_group = next(
            item for item in groups if item is not None and item.sources[0].belief_id.int == 600
        )
        clauses = tuple(
            SummaryClause(text=row.statement, source_ids=(row.id,)) for row in originals
        )
        prepared = await uow.reconsolidation.plan_summary(
            owner(), job.lease_token, group.id, clauses, NOW
        )
        assert prepared is not None
        operation = await uow.reconsolidation.commit_summary(
            owner(), job.lease_token, group.id, prepared, NOW
        )
        copy_clauses = tuple(
            SummaryClause(text=row.statement.swapcase(), source_ids=(UUID(int=600 + i),))
            for i, row in enumerate(originals)
        )
        copy_plan = await uow.reconsolidation.plan_summary(
            owner(), job.lease_token, copy_group.id, copy_clauses[::-1], NOW
        )
        assert copy_plan is not None
        if committed_copy:
            copy_operation = await uow.reconsolidation.commit_summary(
                owner(), job.lease_token, copy_group.id, copy_plan, NOW
            )
            copied = await uow.events.append(
                NewEvent(
                    session_id=SESSION_ID,
                    run_id=None,
                    event_type="context.plan.created",
                    actor_type="system",
                    actor_id="test",
                    payload={"summary_id": str(copy_operation.id), "summary": copy_plan.rendered},
                )
            )
    await service(factory).delete(owner(), operation.id, ceiling=Sensitivity.INTERNAL, key="block")
    async with factory() as uow:
        if committed_copy:
            copy_state = await uow.reconsolidation.summary_operation(owner(), copy_operation.id)
            assert copy_state is not None and copy_state.owner_removed == "delete"
            events = await uow.events.list_after(SESSION_ID, copied.sequence - 1, owner(), limit=1)
            assert copy_plan.rendered not in events[0].model_dump_json()
            return
        assert (
            await uow.reconsolidation.plan_summary(
                owner(), job.lease_token, copy_group.id, copy_clauses, NOW
            )
            is None
        )
        with pytest.raises(ConflictError):
            await uow.reconsolidation.commit_summary(
                owner(), job.lease_token, copy_group.id, copy_plan, NOW
            )
        assert [await uow.memories.get(row.id, owner()) for row in originals] == list(originals)


async def derived_expiry_and_omitted_privacy(factory: UnitOfWorkFactory) -> None:
    operation = await committed_summary(cast(Factory, factory), omitted=True, expires=True)
    clock = FixedClock(NOW)
    api = service(factory, clock=clock)
    await api.get(owner(), operation.id, ceiling=Sensitivity.INTERNAL)
    async with factory() as uow:
        omitted = await uow.memories.get(operation.plan.omitted_source_ids[0], owner())
        await uow.memories.reinforce(
            omitted.model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
        )
    for ceiling in (Sensitivity.INTERNAL, Sensitivity.RESTRICTED):
        with pytest.raises(NotFoundError):
            await api.get(owner(), operation.id, ceiling=ceiling)
        with pytest.raises(NotFoundError):
            await api.review(
                owner(), operation.id, MemoryReviewOutcome.DISMISS, ceiling=ceiling, key="changed"
            )


async def deletion_erases_existing_semantic_copies(factory: UnitOfWorkFactory) -> None:
    await deletion_blocks_new_copies_and_prepared_commits(factory, committed_copy=True)


async def expiry_after_wait_withholds_derived_content(factory: UnitOfWorkFactory) -> None:
    operation = await committed_summary(cast(Factory, factory), expires=True)
    clock = FixedClock(NOW)
    entered = asyncio.Event()
    locked = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def watched() -> AsyncIterator[RepositoryUnitOfWork]:
        async with factory() as uow:
            entered.set()
            yield uow

    async def holder() -> None:
        async with factory() as uow, uow.people.lock(owner()):
            locked.set()
            await release.wait()

    holding = asyncio.create_task(holder())
    await locked.wait()
    reading = asyncio.create_task(
        service(cast(UnitOfWorkFactory, watched), clock=clock).get(
            owner(), operation.id, ceiling=Sensitivity.INTERNAL
        )
    )
    try:
        await entered.wait()
        clock.advance(timedelta(seconds=31))
    finally:
        release.set()
        await holding
    with pytest.raises(NotFoundError):
        await reading


DERIVED_SCENARIOS = (
    browse_defaults_filters_and_keyset,
    review_preserves_evidence_and_historical_scope,
    rejection_delete_and_erased_replays,
    summary_write_and_receipt_rollback,
    simultaneous_summary_retries,
    summary_and_original_write_keys_conflict,
    deletion_blocks_new_copies_and_prepared_commits,
    derived_expiry_and_omitted_privacy,
    deletion_erases_existing_semantic_copies,
    expiry_after_wait_withholds_derived_content,
)
