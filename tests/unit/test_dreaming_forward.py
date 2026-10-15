"""Uncertain legacy People attribution cannot block safe new evidence forever."""

from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.people import InMemoryPeopleStore
from agent_core.application.reconsolidation import PublicReconsolidationService
from agent_core.domain.errors import AuthorizationError
from tests.contract.dreaming_forward_cases import forward_only_sources
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.support import NOW, memory_uow_factory, principal


@pytest.mark.parametrize("legacy", [False, True])
async def test_forward_only_sources_and_erasure(legacy: bool) -> None:
    _, factory = await memory_uow_factory()
    if legacy:
        async with factory() as uow:
            people = cast(InMemoryPeopleStore, uow.people)
            people._erased.add((principal().tenant_id, principal().principal_id, UUID(int=9876)))
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
        people._erased.add((owner.tenant_id, owner.principal_id, UUID(int=9876)))
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
