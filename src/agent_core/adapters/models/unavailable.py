"""Fail-closed provider used when a declared credential is unavailable."""

from __future__ import annotations

from collections.abc import AsyncIterator

from agent_core.adapters.models.common import failed_event
from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest, ResolvedModel


class MissingCredentialProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def stream(
        self,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        del request
        yield failed_event(
            attempt=attempt,
            provider=self.name,
            model=resolved.model,
            sequence=0,
            category="permanent",
            provider_code="credential_missing",
        )

    async def close(self) -> None:
        return
