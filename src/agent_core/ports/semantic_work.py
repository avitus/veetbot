"""Durable semantic indexing admission, independent of dreaming activation."""

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.semantic_memory import SemanticBounds
from agent_core.domain.semantic_work import EmbeddingConfig, SemanticJob, SemanticSpend


class SemanticWorkStore(Protocol):
    async def claim(
        self, principal: Principal, config: EmbeddingConfig, bounds: SemanticBounds, now: datetime
    ) -> SemanticJob | None: ...

    async def current(self, principal: Principal, token: UUID, now: datetime) -> SemanticJob: ...

    async def reserve(
        self,
        principal: Principal,
        token: UUID,
        config: EmbeddingConfig,
        digest: str,
        maximum: Decimal,
        full_cursor: int,
        change_cursor: int,
        now: datetime,
    ) -> SemanticSpend | None: ...

    async def settle(
        self,
        principal: Principal,
        token: UUID,
        spend_id: UUID,
        actual: Decimal | None,
        now: datetime,
    ) -> SemanticSpend: ...

    async def finish(
        self,
        principal: Principal,
        token: UUID,
        full_cursor: int,
        change_cursor: int,
        success: bool,
        now: datetime,
    ) -> SemanticJob: ...

    async def spent(self, principal: Principal, day: date) -> Decimal: ...
