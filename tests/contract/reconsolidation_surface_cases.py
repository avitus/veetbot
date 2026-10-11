"""Owner-visible history, redaction, cursors and durable undo across both stores."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.reconsolidation import PublicReconsolidationService
from agent_core.domain.agents import Principal
from agent_core.domain.errors import AuthorizationError, ConflictError, NotFoundError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.reconsolidation_views import (
    OperationKind,
    OperationState,
    ReconsolidationValidationError,
)
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.reconsolidation_operation_cases import committed
from tests.contract.reconsolidation_source_cases import seed_sources
from tests.contract.reconsolidation_summary_recall_cases import summary_recall_inputs
from tests.contract.support import NOW, principal


def owner() -> Principal:
    return principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})


def service(factory: Factory) -> PublicReconsolidationService:
    return PublicReconsolidationService(cast(UnitOfWorkFactory, factory), FixedClock(NOW))


async def operation_inspection(factory: Factory) -> None:
    _, _, operation = await committed(factory)
    reader = service(factory)
    view = await reader.get(owner(), operation.id, ceiling=Sensitivity.INTERNAL)
    assert view.id == operation.id and view.kind == "merge" and view.state == "committed"
    assert view.content is not None and view.content.memory_id == operation.plan.canonical_id
    assert tuple(s.belief_id for s in view.sources) == operation.plan.member_ids
    assert set(view.model_dump()) == {
        "id",
        "kind",
        "state",
        "revision",
        "reason",
        "policy",
        "model_identity",
        "created_at",
        "committed_at",
        "invalidated_at",
        "undone_at",
        "content",
        "sources",
    }
    listing = await reader.list(owner(), ceiling=Sensitivity.INTERNAL)
    assert listing.items == [view] and listing.next_cursor is None
    for foreign in (
        owner().model_copy(update={"principal_id": "other"}),
        owner().model_copy(update={"tenant_id": "other"}),
    ):
        with pytest.raises(NotFoundError):
            await reader.get(foreign, operation.id, ceiling=Sensitivity.RESTRICTED)
        assert not (await reader.list(foreign, ceiling=Sensitivity.RESTRICTED)).items
    with pytest.raises(NotFoundError):
        await reader.get(owner(), operation.id, ceiling=Sensitivity.PUBLIC)


async def operation_undo_replay_erasure(factory: Factory) -> None:
    _, _, operation = await committed(factory)
    reader = service(factory)
    undone = await reader.undo(
        owner(), operation.id, ceiling=Sensitivity.INTERNAL, expected_revision=1, key="undo"
    )
    assert undone.state == "undone" and undone.revision == 2 and undone.content is not None
    with pytest.raises(ConflictError):
        await reader.undo(
            owner(), operation.id, ceiling=Sensitivity.RESTRICTED, expected_revision=2, key="undo"
        )
    with pytest.raises(ConflictError):
        await reader.undo(
            owner(),
            operation.id,
            ceiling=Sensitivity.RESTRICTED,
            expected_revision=1,
            key="different",
        )
    async with factory() as uow:
        await uow.memories.fence_for_erasure(owner(), [operation.plan.member_ids[-1]])
    replay = await reader.undo(
        owner(), operation.id, ceiling=Sensitivity.RESTRICTED, expected_revision=1, key="undo"
    )
    assert replay.state == "undone" and replay.revision == 2
    assert replay.content is None and not replay.sources
    with pytest.raises(NotFoundError):
        await reader.get(owner(), operation.id, ceiling=Sensitivity.INTERNAL)


async def operation_summary_complete_support(factory: Factory) -> None:
    operation, originals = await summary_recall_inputs(
        cast(UnitOfWorkFactory, factory), omit_last=True
    )
    reader = service(factory)
    view = await reader.get(owner(), operation.id, ceiling=Sensitivity.INTERNAL)
    assert view.content is not None and view.content.clauses
    assert [s.belief_id for s in view.sources if s.omitted] == [originals[-1].id]
    with pytest.raises(ConflictError):
        await reader.undo(
            owner(),
            operation.id,
            ceiling=Sensitivity.INTERNAL,
            expected_revision=1,
            key="wrong-kind",
        )
    async with factory() as uow:
        await uow.memories.fence_for_erasure(owner(), [originals[-1].id])
    hidden = await reader.get(owner(), operation.id, ceiling=Sensitivity.RESTRICTED)
    assert hidden.state == "invalidated" and hidden.content is None and hidden.sources == ()
    assert originals[-1].statement not in hidden.model_dump_json()
    assert not (await reader.list(owner(), ceiling=Sensitivity.INTERNAL)).items


async def operation_scope_and_validation(factory: Factory) -> None:
    _, _, operation = await committed(factory)
    reader = service(factory)
    with pytest.raises(AuthorizationError):
        await reader.get(principal(), operation.id, ceiling=Sensitivity.RESTRICTED)
    with pytest.raises(AuthorizationError):
        await reader.list(principal(), ceiling=Sensitivity.RESTRICTED)
    with pytest.raises(AuthorizationError):
        await reader.undo(
            principal(),
            operation.id,
            ceiling=Sensitivity.RESTRICTED,
            expected_revision=1,
            key="undo",
        )
    for key in ("", " " * 3, "x" * 129):
        with pytest.raises(ReconsolidationValidationError):
            await reader.undo(
                owner(), operation.id, ceiling=Sensitivity.RESTRICTED, expected_revision=1, key=key
            )
    for cursor in ("garbage", "e30", "x" * 2049):
        with pytest.raises(ReconsolidationValidationError):
            await reader.list(owner(), ceiling=Sensitivity.RESTRICTED, cursor=cursor)
    assert not (await reader.list(owner(), ceiling=Sensitivity.RESTRICTED, kind="summary")).items
    assert not (await reader.list(owner(), ceiling=Sensitivity.RESTRICTED, state="undone")).items
    with pytest.raises(NotFoundError):
        await reader.get(owner(), UUID(int=99999), ceiling=Sensitivity.RESTRICTED)


SURFACE_SCENARIOS = [
    operation_inspection,
    operation_undo_replay_erasure,
    operation_summary_complete_support,
    operation_scope_and_validation,
]


async def operation_cursor_visibility_and_binding(factory: Factory) -> None:
    async with factory() as uow:
        await seed_sources(uow)
        for i in range(2, 6):
            await uow.memories.upsert_belief(
                memory(belief_id=501 + i).model_copy(
                    update={
                        "store_position": await uow.memories.next_position(),
                    }
                )
            )
        job = await uow.reconsolidation.claim_due(owner(), NOW, "pages")
        assert job is not None
        inventory = await uow.reconsolidation.inventory(owner(), job.lease_token, NOW)
        await uow.reconsolidation.checkpoint(
            owner(),
            job.lease_token,
            inventory,
            tuple(inventory.sources[i : i + 2] for i in range(0, 6, 2)),
            NOW,
        )
        operations = []
        for _ in range(3):
            group = await uow.reconsolidation.claim_group(owner(), job.lease_token, NOW)
            assert group is not None
            plan = await uow.reconsolidation.plan_merge(owner(), job.lease_token, group.id, NOW)
            assert plan is not None
            operations.append(
                await uow.reconsolidation.commit_merge(
                    owner(), job.lease_token, group.id, plan, NOW
                )
            )
        await uow.memories.fence_for_erasure(owner(), [UUID(int=503)])
        with pytest.raises(ValueError):
            await uow.reconsolidation.operation_page(
                owner(), kind=None, state=None, before=None, limit=101
            )
    reader = service(factory)
    expected = sorted(
        (op.id for op in operations if UUID(int=503) not in op.plan.member_ids), reverse=True
    )
    page = await reader.list(owner(), ceiling=Sensitivity.INTERNAL, limit=1)
    assert [v.id for v in page.items] == expected[:1] and page.next_cursor is not None
    cursor = page.next_cursor
    last = await reader.list(owner(), ceiling=Sensitivity.INTERNAL, limit=1, cursor=cursor)
    assert [v.id for v in last.items] == expected[1:] and last.next_cursor is None
    assert await reader.list(owner(), ceiling=Sensitivity.INTERNAL, limit=1, cursor=cursor) == last
    for principal_value, ceiling, kind, state in (
        (owner().model_copy(update={"principal_id": "other"}), Sensitivity.INTERNAL, None, None),
        (owner(), Sensitivity.RESTRICTED, None, None),
        (owner(), Sensitivity.INTERNAL, "merge", None),
        (owner(), Sensitivity.INTERNAL, None, "committed"),
    ):
        with pytest.raises(ReconsolidationValidationError):
            await reader.list(
                principal_value,
                ceiling=ceiling,
                kind=cast(OperationKind | None, kind),
                state=cast(OperationState | None, state),
                cursor=cursor,
            )


async def operation_state_filter_revalidates_expiry(factory: Factory) -> None:
    operation, _ = await summary_recall_inputs(
        cast(UnitOfWorkFactory, factory), expires_at=NOW + timedelta(seconds=1)
    )
    reader = PublicReconsolidationService(
        cast(UnitOfWorkFactory, factory), FixedClock(NOW + timedelta(seconds=2))
    )
    result = await reader.list(owner(), ceiling=Sensitivity.RESTRICTED, state="invalidated")
    assert [v.id for v in result.items] == [operation.id], (
        "state filtering must follow dependency revalidation"
    )
    assert result.items[0].content is None and not result.items[0].sources


async def operation_concurrent_write_only_undo(factory: Factory) -> None:
    _, _, operation = await committed(factory)
    reader = service(factory)
    writer = owner().model_copy(update={"scopes": {"memory.write"}})
    results = await asyncio.gather(
        *(
            reader.undo(
                writer, operation.id, ceiling=Sensitivity.INTERNAL, expected_revision=1, key="race"
            )
            for _ in range(2)
        )
    )
    assert results[0] == results[1]
    assert results[0].revision == 2 and results[0].state == "undone"
    with pytest.raises(AuthorizationError):
        await reader.get(writer, operation.id, ceiling=Sensitivity.INTERNAL)


async def operation_expiry_after_transaction_wait(factory: Factory) -> None:
    operation, _ = await summary_recall_inputs(
        cast(UnitOfWorkFactory, factory), expires_at=NOW + timedelta(seconds=1)
    )
    clock = FixedClock(NOW)

    @asynccontextmanager
    async def waited() -> AsyncIterator[Stores]:
        async with factory() as uow, uow.people.lock(owner()):
            # Model time elapsed while acquiring a connection and the owner guard.
            clock.advance(timedelta(seconds=2))
            yield uow

    reader = PublicReconsolidationService(cast(UnitOfWorkFactory, waited), clock)
    result = await reader.list(owner(), ceiling=Sensitivity.RESTRICTED, state="invalidated")
    assert [v.id for v in result.items] == [operation.id], (
        "current visibility must use the clock after the transaction/owner wait"
    )
    assert result.items[0].content is None


SURFACE_SCENARIOS.extend(
    [
        operation_cursor_visibility_and_binding,
        operation_state_filter_revalidates_expiry,
        operation_concurrent_write_only_undo,
        operation_expiry_after_transaction_wait,
    ]
)
