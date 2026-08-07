"""Provider-neutral model adapter port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from agent_core.domain.messages import (
    CapabilitySet,
    ModelAttempt,
    ModelEvent,
    ModelRequest,
    ProviderPin,
    ResolvedModel,
)


class ModelProvider(Protocol):
    name: str

    def stream(
        self,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]: ...

    async def close(self) -> None: ...


class ModelRouter(Protocol):
    async def resolve(
        self,
        model_policy: str,
        *,
        tenant_id: str,
        required: CapabilitySet | None = None,
    ) -> ResolvedModel: ...

    async def resolve_pinned(self, pin: ProviderPin) -> ResolvedModel: ...

    def pin(self, run_id: UUID, resolved: ResolvedModel) -> ProviderPin: ...
