"""Uncertain legacy People attribution cannot block safe new evidence forever."""

import hashlib
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.people import InMemoryPeopleStore
from agent_core.application.reconsolidation import PublicReconsolidationService
from agent_core.domain.errors import AuthorizationError, ConflictError
from agent_core.domain.people import Person
from tests.contract.dreaming_forward_cases import forward_only_sources
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.support import NOW, memory_uow_factory, principal


@pytest.mark.parametrize("legacy", [False, True])
async def test_forward_only_sources_and_erasure(legacy: bool) -> None:
    _, factory = await memory_uow_factory()
    if legacy:
        async with factory() as uow:
            people = cast(InMemoryPeopleStore, uow.people)
            await _unknown_erased_person(people, UUID(int=9876))
            assert not await people.memory_attribution_complete(principal())
    await forward_only_sources(cast(Factory, factory))
    async with factory() as uow:
        people = cast(InMemoryPeopleStore, uow.people)
        if legacy:
            assert not await people.memory_attribution_complete(principal())
        assert await people.dreaming_attribution_ready(principal())
        people._erased.add((principal().tenant_id, principal().principal_id, UUID(int=9877)))
        assert not await people.dreaming_attribution_ready(principal())


async def test_forward_cutover_requires_scope_and_exposes_only_date() -> None:
    _, factory = await memory_uow_factory()
    service = PublicReconsolidationService(factory, FixedClock(NOW))
    with pytest.raises(AuthorizationError):
        await service.enable_forward_dreaming(principal().model_copy(update={"scopes": set()}))
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    boundary = await service.enable_forward_dreaming(owner)
    assert await service.enable_forward_dreaming(owner) == boundary
    status = await service.dreaming_status(owner)
    assert status.forward_only_since == NOW and status.blockers == ()
    assert "legacy_digest" not in status.model_dump_json()


async def test_forward_cutover_does_not_change_inflight_admission_or_schedule() -> None:
    from datetime import timedelta

    from agent_core.domain.errors import ConflictError

    _, factory = await memory_uow_factory()
    clock = FixedClock(NOW)
    service = PublicReconsolidationService(factory, clock)
    owner = principal().model_copy(update={"scopes": {"memory.read", "memory.write"}})
    async with factory() as uow:
        before = await uow.reconsolidation.dreaming_schedule(owner)
        job = await uow.reconsolidation.claim_due(owner, NOW, "inflight")
        assert job is not None
    with pytest.raises(ConflictError, match="Dreaming is running"):
        await service.enable_forward_dreaming(owner)
    async with factory() as uow:
        assert await uow.people.forward_dreaming_boundary(owner) is None
        await uow.reconsolidation.release(owner, job.lease_token, NOW)
    boundary = await service.enable_forward_dreaming(owner)
    clock.advance(timedelta(days=1))
    assert await service.enable_forward_dreaming(owner) == boundary
    async with factory() as uow:
        assert await uow.reconsolidation.dreaming_schedule(owner) == before


async def test_forward_only_legacy_owner_reaches_provider_review() -> None:
    from agent_core.memory.reconsolidation_execution import ReconsolidationBatchExecutor
    from agent_core.ports.persistence import UnitOfWorkFactory
    from tests.contract.reconsolidation_admission_cases import model, policy
    from tests.contract.reconsolidation_execution_cases import ExecutionProvider, TrackedFactory
    from tests.contract.reconsolidation_source_cases import seed

    _, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"memory.write"}})
    async with factory() as uow:
        people = cast(InMemoryPeopleStore, uow.people)
        await _unknown_erased_person(people, UUID(int=9876))
    await PublicReconsolidationService(factory, FixedClock(NOW)).enable_forward_dreaming(owner)
    tracked = TrackedFactory(cast(Factory, factory))
    async with tracked() as uow:
        job, group = await seed(uow)
    executor = ReconsolidationBatchExecutor(
        cast(UnitOfWorkFactory, tracked),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    provider = ExecutionProvider(tracked)
    result = await executor.run(
        job, (group.id,), model=model(), provider=provider, batch_id=UUID(int=90)
    )
    assert result.reason == "reviewed" and result.review is not None
    assert len(provider.requests) == 2


async def test_cutover_retires_queued_legacy_groups_without_deleting_history() -> None:
    from agent_core.adapters.memory.reconsolidation import InMemoryReconsolidationStore
    from tests.contract.reconsolidation_source_cases import seed

    _, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"memory.write"}})
    async with factory() as uow:
        job, group = await seed(cast(Stores, uow))
        await uow.reconsolidation.finish_group(principal(), job.lease_token, group.id, "retry", NOW)
        await uow.reconsolidation.release(principal(), job.lease_token, NOW)
    await PublicReconsolidationService(factory, FixedClock(NOW)).enable_forward_dreaming(owner)
    async with factory() as uow:
        store = cast(InMemoryReconsolidationStore, uow.reconsolidation)
        retained = store._groups[group.id]
        assert retained.state == "stale" and retained.reason == "source_changed"
        assert retained.sources == group.sources and retained.attempts == group.attempts
        assert await uow.memories.get(group.sources[0].belief_id, principal()) is not None


async def test_forward_cutover_refuses_live_unknown_people_before_and_after_activation() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        people = cast(InMemoryPeopleStore, uow.people)
        owner = principal()
        record = Person(
            id=UUID(int=9880),
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            display_name="Fabricated legacy person",
            created_at=NOW,
            updated_at=NOW,
        )
        await people.put(record, expected_revision=0)
        footprint = people._attributions.pop((owner.tenant_id, owner.principal_id, record.id))
        with pytest.raises(ConflictError, match="Unattributed live People evidence"):
            await people.enable_forward_dreaming(owner, 0, NOW)
        assert await people.forward_dreaming_boundary(owner) is None
        foreign = owner.model_copy(update={"principal_id": "foreign"})
        await people.enable_forward_dreaming(foreign, 0, NOW)
        assert await people.dreaming_attribution_ready(foreign)
        key = (owner.tenant_id, owner.principal_id, record.id)
        people._attributions[key] = footprint
        await people.enable_forward_dreaming(owner, 0, NOW)
        del people._attributions[key]
        assert not await people.dreaming_attribution_ready(owner)


@pytest.mark.parametrize("fence_only", [False, True])
async def test_forward_digest_keeps_erased_head_identity_after_payload_purge(
    fence_only: bool,
) -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        people = cast(InMemoryPeopleStore, uow.people)
        owner = principal()
        record = Person(
            id=UUID(int=9881),
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            display_name="Fabricated erased person",
            created_at=NOW,
            updated_at=NOW,
        )
        await people.put(record, expected_revision=0)
        record = record.model_copy(update={"revision": 2})
        await people.put(record, expected_revision=1)
        key = (owner.tenant_id, owner.principal_id, record.id)
        del people._attributions[key]
        if fence_only:
            await people.fence_for_erasure(owner, [record.id])
        else:
            await people.erase(owner, [record.id])
        boundary = await people.enable_forward_dreaming(owner, 0, NOW)
        expected = hashlib.sha256(f"{record.id}:2:person".encode()).hexdigest()
        assert boundary.legacy_digest == expected
        while await people.purge_erased(owner, limit=1):
            assert await people.dreaming_attribution_ready(owner)
        assert key not in people._records
        assert await people.dreaming_attribution_ready(owner)


async def _unknown_erased_person(people: InMemoryPeopleStore, record_id: UUID) -> None:
    owner = principal()
    await people.put(
        Person(
            id=record_id,
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            display_name="Fabricated legacy person",
            created_at=NOW,
            updated_at=NOW,
        ),
        expected_revision=0,
    )
    await people.erase(owner, [record_id])
    del people._attributions[(owner.tenant_id, owner.principal_id, record_id)]


@pytest.mark.parametrize("identity", [(2, "person"), (1, "memory_link")])
async def test_forward_digest_rejects_changed_erased_head_identity(
    identity: tuple[int, str],
) -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        people = cast(InMemoryPeopleStore, uow.people)
        await _unknown_erased_person(people, UUID(int=9882))
        await people.enable_forward_dreaming(principal(), 0, NOW)
        people._erased_heads[(principal().tenant_id, principal().principal_id, UUID(int=9882))] = (
            identity
        )
        assert not await people.dreaming_attribution_ready(principal())


async def test_forward_cutover_refuses_missing_erased_head_identity() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        people = cast(InMemoryPeopleStore, uow.people)
        people._erased.add((principal().tenant_id, principal().principal_id, UUID(int=9883)))
        with pytest.raises(ConflictError, match="head identity is unavailable"):
            await people.enable_forward_dreaming(principal(), 0, NOW)
        assert await people.forward_dreaming_boundary(principal()) is None


async def test_erased_head_metadata_rolls_back_and_is_removed_with_owner() -> None:
    _, factory = await memory_uow_factory()
    key = (principal().tenant_id, principal().principal_id, UUID(int=9884))
    with pytest.raises(RuntimeError, match="Rollback"):
        async with factory() as uow:
            people = cast(InMemoryPeopleStore, uow.people)
            await _unknown_erased_person(people, key[2])
            assert key in people._erased_heads
            raise RuntimeError("Rollback")
    async with factory() as uow:
        people = cast(InMemoryPeopleStore, uow.people)
        assert key not in people._erased_heads and key not in people._erased
        await _unknown_erased_person(people, key[2])
        await people.erase_principal(principal())
        assert key not in people._erased_heads and key not in people._erased
