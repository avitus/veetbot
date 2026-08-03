"""Provider-neutral model adapter port."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from agent_core.domain.messages import (
    ModelAttempt,
    ModelEvent,
    ModelRequest,
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
