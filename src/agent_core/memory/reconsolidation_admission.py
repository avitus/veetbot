"""Fresh source checks and durable spend admission for the maintenance worker."""

import hashlib
from collections.abc import Callable
from decimal import Decimal
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation import CallAdmission, ReconsolidationSpend
from agent_core.domain.reconsolidation_inputs import (
    InputDeferral,
    PreparedProposalRequest,
    PreparedVerificationRequest,
    ProposalPreparation,
    ReconsolidationInput,
    VerificationPreparation,
)
from agent_core.memory.reconsolidation_inputs import EgressPolicy, prepare_proposal_request
from agent_core.memory.reconsolidation_verification import prepare_verification_request
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class ReconsolidationRequestAdmission:
    """Return only after the source read and spend reservation transaction commits.

    This is a worker-facing seam, not a transport or production activation hook.
    The caller must send outside the unit of work and retain settlement on failure.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        principal: Principal,
        *,
        admitted: Callable[[], bool],
        egress_policy: EgressPolicy,
    ) -> None:
        self._factory = uow_factory
        self._clock = clock
        self._principal = principal
        self._admitted = admitted
        self._policy = egress_policy

    def _check_admission(self) -> None:
        if not self._admitted():
            raise ConflictError("reconsolidation admission withdrawn")

    async def _snapshots(
        self, uow: RepositoryUnitOfWork, token: UUID, group_ids: tuple[UUID, ...]
    ) -> tuple[ReconsolidationInput, ...]:
        if not 1 <= len(group_ids) <= 4 or len(set(group_ids)) != len(group_ids):
            raise ValueError("request needs one through four distinct claimed groups")
        groups = []
        for group_id in group_ids:
            value = await uow.reconsolidation.original_input(
                self._principal, token, group_id, self._clock.now()
            )
            if value is not None:
                groups.append(value)
        return tuple(groups)

    async def _reserve(
        self, uow: RepositoryUnitOfWork, prepared: PreparedProposalRequest | None
    ) -> ReconsolidationSpend | None:
        self._check_admission()
        if prepared is None:
            return None
        now = self._clock.now()
        metadata = prepared.request.metadata
        return await uow.reconsolidation.reserve(
            self._principal,
            prepared.lease_token,
            prepared.request_digest,
            prepared.maximum_cost_usd,
            now,
            admission=CallAdmission(
                batch_id=prepared.context.batch_id,
                stage="verification"
                if isinstance(prepared, PreparedVerificationRequest)
                else "proposal",
                group_ids=tuple(group.id for group in prepared.context.groups),
                provider=metadata["provider"],
                model=metadata["model"],
                pricing_digest=metadata["pricing_digest"],
                egress_policy_digest=hashlib.sha256(
                    prepared.egress_policy_version.encode()
                ).hexdigest(),
                admitted_at=now,
            ),
        )

    async def proposal(
        self,
        token: UUID,
        group_ids: tuple[UUID, ...],
        *,
        model: ResolvedModel,
        batch_id: UUID,
    ) -> tuple[ProposalPreparation, ReconsolidationSpend | None]:
        self._check_admission()
        # Both adapters acquire the memory owner guard before the People lock.
        # Keep source reads stable through local policy and reservation.
        async with self._factory() as uow, uow.people.lock(self._principal):
            groups = await self._snapshots(uow, token, group_ids)
            result = prepare_proposal_request(
                self._principal,
                groups,
                model=model,
                egress_policy=self._policy,
                batch_id=batch_id,
                now=self._clock.now(),
            )
            found = {g.group.id for g in groups}
            missing = tuple(
                InputDeferral(group_id=key, reason="source_unavailable")
                for key in group_ids
                if key not in found
            )
            result = result.model_copy(update={"deferred": (*result.deferred, *missing)})
            reservation = await self._reserve(uow, result.prepared)
        return result, reservation

    async def verification(
        self,
        original: PreparedProposalRequest,
        proposal_json: str,
        *,
        model: ResolvedModel,
        remaining_usd: Decimal,
    ) -> tuple[VerificationPreparation, ReconsolidationSpend | None]:
        self._check_admission()
        if (original.context.tenant_id, original.context.principal_id) != (
            self._principal.tenant_id,
            self._principal.principal_id,
        ):
            raise ConflictError("reconsolidation request owner changed")
        async with self._factory() as uow, uow.people.lock(self._principal):
            groups = await self._snapshots(
                uow, original.lease_token, tuple(g.id for g in original.context.groups)
            )
            result = prepare_verification_request(
                self._principal,
                original,
                proposal_json,
                groups,
                model=model,
                egress_policy=self._policy,
                remaining_usd=remaining_usd,
                now=self._clock.now(),
            )
            reservation = await self._reserve(uow, result.prepared)
        return result, reservation
