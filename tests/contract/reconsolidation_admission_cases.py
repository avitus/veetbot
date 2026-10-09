"""Request admission reads and reserves through either real transactional adapter."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import ModelPricing, ResolvedModel
from agent_core.domain.reconsolidation_inputs import EgressSubject, ReconsolidationEgressDecision
from agent_core.memory.reconsolidation_admission import ReconsolidationRequestAdmission
from agent_core.memory.reconsolidation_provider import proposal_digest, review_prepared_verification
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.reconsolidation_cases import Factory, Stores
from tests.contract.reconsolidation_input_cases import named
from tests.contract.reconsolidation_source_cases import seed
from tests.contract.support import NOW, principal


def policy(
    owner: Principal, model: ResolvedModel, subject: EgressSubject, now: datetime
) -> ReconsolidationEgressDecision:
    return ReconsolidationEgressDecision(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        provider=model.provider,
        model=model.model,
        policy_version="fixture-egress@1",
        assessed_at=now,
        permitted=True,
        sensitivity=Sensitivity.INTERNAL,
        residency="allowed",
    )


def model() -> ResolvedModel:
    return ResolvedModel(
        provider="fake",
        model="test-model",
        resolved_at=NOW,
        pricing=ModelPricing(input_per_mtok=Decimal(1), output_per_mtok=Decimal(2)),
    )


async def admission_reserves_both_exact_requests(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    service = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    first, reservation = await service.proposal(
        job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90)
    )
    assert first.prepared is not None and reservation is not None, (
        "a worker request needs a durable reservation"
    )
    assert reservation.request_digest == first.prepared.request_digest
    assert reservation.maximum_usd == first.prepared.maximum_cost_usd
    offered = first.prepared.context.groups[0]
    response = json.dumps(
        {
            "schema_version": "reconsolidation-proposal@1",
            "batch_id": str(first.prepared.context.batch_id),
            "group_ids": [str(group.id)],
            "operations": [
                {
                    "id": str(UUID(int=91)),
                    "group_id": str(group.id),
                    "kind": "no_change",
                    "inputs": [s.source.model_dump(mode="json") for s in offered.sources],
                    "clauses": [],
                }
            ],
        }
    )
    async with factory() as uow:
        await uow.reconsolidation.settle(
            principal(), job.lease_token, reservation.id, Decimal(0), NOW
        )
    second, verification_spend = await service.verification(
        first.prepared, response, model=model(), remaining_usd=Decimal("0.25")
    )
    assert second.prepared is not None and verification_spend is not None
    assert verification_spend.request_digest == second.prepared.request_digest
    assert verification_spend.request_digest != reservation.request_digest
    checked = review_prepared_verification(
        second.prepared,
        json.dumps(
            {
                "schema_version": "reconsolidation-verification@1",
                "batch_id": str(first.prepared.context.batch_id),
                "proposal_digest": proposal_digest(second.prepared.proposal),
                "verdicts": [],
            }
        ),
    )
    assert checked.candidates[0].requires_local_validation
    async with factory() as uow:
        current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        assert current.requests == 2 and current.slice_spent == verification_spend.maximum_usd


ADMISSION_SCENARIOS: list[Callable[[Factory], Awaitable[None]]] = [
    admission_reserves_both_exact_requests
]


async def admission_policy_denial_preserves_spend(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)

    def deny(
        owner: Principal, resolved: ResolvedModel, subject: EgressSubject, at: datetime
    ) -> ReconsolidationEgressDecision:
        return policy(owner, resolved, subject, at).model_copy(update={"permitted": False})

    service = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=deny,
    )
    result, spend = await service.proposal(
        job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90)
    )
    assert (
        result.prepared is None and spend is None and result.deferred[0].reason == "egress_denied"
    )
    async with factory() as uow:
        assert (await uow.reconsolidation.renew(principal(), job.lease_token, NOW)).requests == 0


async def admission_disabled_or_revoked(factory: Factory, *, during: bool) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    enabled = during

    def revoke(
        owner: Principal, resolved: ResolvedModel, subject: EgressSubject, at: datetime
    ) -> ReconsolidationEgressDecision:
        nonlocal enabled
        enabled = False
        return policy(owner, resolved, subject, at)

    service = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: enabled,
        egress_policy=revoke,
    )
    with pytest.raises(ConflictError, match="admission withdrawn"):
        await service.proposal(job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90))
    async with factory() as uow:
        assert (await uow.reconsolidation.renew(principal(), job.lease_token, NOW)).requests == 0


async def admission_duplicate_request_is_not_a_second_send(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    service = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    result, spend = await service.proposal(
        job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90)
    )
    assert result.prepared is not None and spend is not None
    with pytest.raises(ConflictError, match="already reserved"):
        await service.proposal(job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90))
    async with factory() as uow:
        current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        assert current.requests == 1 and current.slice_spent == spend.maximum_usd


async def admission_reservation_failure_rolls_back(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
        await uow.reconsolidation.reserve(
            principal(), job.lease_token, "0" * 64, Decimal("0.249"), NOW
        )
    service = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    with pytest.raises(ConflictError, match="budget exhausted"):
        await service.proposal(job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90))
    async with factory() as uow:
        current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        assert current.requests == 1 and current.slice_spent == Decimal("0.249")


async def admission_transaction_failure_returns_no_permit(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)

    @asynccontextmanager
    async def aborting() -> AsyncIterator[Stores]:
        async with factory() as uow:
            yield uow
            raise RuntimeError("synthetic commit failure")

    service = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, aborting),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        await service.proposal(job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90))
    async with factory() as uow:
        current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        assert current.requests == 0 and current.slice_spent == 0


async def admission_wait_checks_current_sources_and_time(
    factory: Factory, *, deadline: bool
) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    clock = FixedClock(NOW)
    service = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        clock,
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    async with factory() as uow, uow.people.lock(principal()):
        task = asyncio.create_task(
            service.proposal(job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90))
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        if deadline:
            clock.advance(timedelta(seconds=120))
        else:
            await uow.memories.fence_for_erasure(principal(), [group.sources[0].belief_id])
    if deadline:
        with pytest.raises(ConflictError, match="deadline"):
            await task
    else:
        result, spend = await task
        assert result.prepared is None and spend is None
        assert result.deferred[0].reason == "source_unavailable"
    async with factory() as uow:
        assert (await uow.reconsolidation.renew(principal(), job.lease_token, NOW)).requests == 0


async def admission_verification_rechecks_erased_source(factory: Factory) -> None:
    async with factory() as uow:
        job, group = await seed(uow)
    service = ReconsolidationRequestAdmission(
        cast(UnitOfWorkFactory, factory),
        FixedClock(NOW),
        principal(),
        admitted=lambda: True,
        egress_policy=policy,
    )
    first, spend = await service.proposal(
        job.lease_token, (group.id,), model=model(), batch_id=UUID(int=90)
    )
    assert first.prepared is not None and spend is not None
    response = json.dumps(
        {
            "schema_version": "reconsolidation-proposal@1",
            "batch_id": str(UUID(int=90)),
            "group_ids": [str(group.id)],
            "operations": [
                {
                    "id": str(UUID(int=91)),
                    "group_id": str(group.id),
                    "kind": "no_change",
                    "inputs": [
                        s.source.model_dump(mode="json")
                        for s in first.prepared.context.groups[0].sources
                    ],
                    "clauses": [],
                }
            ],
        }
    )
    async with factory() as uow:
        await uow.memories.fence_for_erasure(principal(), [group.sources[0].belief_id])
    second, reservation = await service.verification(
        first.prepared, response, model=model(), remaining_usd=Decimal("0.25")
    )
    assert second.prepared is None and reservation is None
    async with factory() as uow:
        current = await uow.reconsolidation.renew(principal(), job.lease_token, NOW)
        assert current.requests == 1 and current.slice_spent == spend.maximum_usd


ADMISSION_SCENARIOS += [
    admission_policy_denial_preserves_spend,
    named(admission_disabled_or_revoked, "admission_initially_disabled", during=False),
    named(admission_disabled_or_revoked, "admission_revoked_during_preparation", during=True),
    admission_duplicate_request_is_not_a_second_send,
    admission_reservation_failure_rolls_back,
    admission_transaction_failure_returns_no_permit,
    named(
        admission_wait_checks_current_sources_and_time,
        "admission_wait_rechecks_deadline",
        deadline=True,
    ),
    named(
        admission_wait_checks_current_sources_and_time,
        "admission_wait_rechecks_erasure",
        deadline=False,
    ),
    admission_verification_rechecks_erased_source,
]
