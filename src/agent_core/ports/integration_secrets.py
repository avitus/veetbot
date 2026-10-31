"""Owner-scoped sealed storage; writes hold the owner lock through their audit."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.integration_secrets import IntegrationSecret


class IntegrationSecretStore(Protocol):
    def locked(self, principal: Principal) -> AbstractAsyncContextManager[None]: ...
    async def get(self, principal: Principal, key_id: UUID) -> IntegrationSecret | None: ...
    async def list(self, principal: Principal) -> list[IntegrationSecret]: ...
    async def put(self, principal: Principal, record: IntegrationSecret) -> None: ...
