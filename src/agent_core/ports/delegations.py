"""Delegation-ledger repository port."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.delegations import Delegation, DelegationStatus


class DelegationRepository(Protocol):
    """Store the ledger rows that link a suspended parent invocation to its children."""

    async def create(self, delegation: Delegation) -> Delegation: ...

    async def get(self, delegation_id: UUID, principal: Principal) -> Delegation: ...

    async def get_by_invocation(self, invocation_id: UUID) -> Delegation | None: ...

    async def get_for_parent_run(self, parent_run_id: UUID) -> list[Delegation]: ...

    async def live_children_for_parent(self, parent_run_id: UUID) -> int: ...

    async def live_children_for_tenant(self, tenant_id: str) -> int: ...

    async def transition(
        self,
        delegation_id: UUID,
        expected: DelegationStatus,
        updated: Delegation,
    ) -> Delegation: ...
