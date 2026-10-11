"""Local derived index; provider calls and activation are separate concerns."""

from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.memory import RecallQuery
from agent_core.domain.semantic_memory import (
    EmbeddingSpace,
    SemanticBounds,
    SemanticHit,
    SemanticPage,
    SemanticQueryVector,
    SemanticSource,
)


class SemanticMemoryIndex(Protocol):
    async def capture(
        self, principal: Principal, belief_id: UUID, space: EmbeddingSpace
    ) -> SemanticSource | None:
        """Capture a current live original and its source version, without egress."""
        ...

    async def put(
        self, principal: Principal, source: SemanticSource, vector: tuple[float, ...]
    ) -> bool:
        """CAS against the current original; a stale response must never resurrect it."""
        ...

    async def search(
        self, query: RecallQuery, space: EmbeddingSpace, vector: tuple[float, ...], *, limit: int
    ) -> list[SemanticHit]:
        """Exact current-source search with hard predicates before the bounded limit.

        Historical queries return no dense results until historical vectors are
        separately admitted; callers retain their existing historical sparse path.
        """
        ...

    async def bounds(self, principal: Principal) -> SemanticBounds: ...

    async def inventory(
        self, principal: Principal, *, after: int, through: int, limit: int = 64
    ) -> SemanticPage:
        """Finite insertion-order sweep. Cursor advances over deleted/ineligible rows."""
        ...

    async def changes(
        self, principal: Principal, *, after: int, through: int, limit: int = 64
    ) -> SemanticPage:
        """Bounded opaque source journal, independent of reconsolidation job activation."""
        ...


class SemanticQueryLookup(Protocol):
    def lookup(self, query: RecallQuery) -> SemanticQueryVector | None:
        """Bounded local lookup only: no network, model call, source text or evidence cache.

        Misses retain sparse recall. Full query identity binds owner, scope,
        sensitivity, exclusions and the task; current sources are rejoined later.
        Production composition remains absent until activation evidence passes.
        """
        ...
