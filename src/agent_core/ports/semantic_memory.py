"""Local derived index; provider calls and activation are separate concerns."""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.memory import RecallQuery
from agent_core.domain.reconsolidation import SourceVersion
from agent_core.domain.semantic_memory import (
    EmbeddingSpace,
    SemanticBounds,
    SemanticHit,
    SemanticPage,
    SemanticQueryVector,
    SemanticSource,
)


class SemanticMemoryIndex(Protocol):
    async def capture_revision(
        self, principal: Principal, source: SourceVersion, space: EmbeddingSpace
    ) -> SemanticSource | None:
        """Capture a historical original under its current source/visibility fence."""
        ...

    async def capture(
        self, principal: Principal, belief_id: UUID, space: EmbeddingSpace
    ) -> SemanticSource | None:
        """Capture a current live original and its source version, without egress."""
        ...

    async def indexed(self, principal: Principal, source: SemanticSource) -> bool:
        """Whether this exact source/space already has a valid fenced vector."""
        ...

    async def put(
        self, principal: Principal, source: SemanticSource, vector: tuple[float, ...]
    ) -> bool:
        """CAS against the current original; a stale response must never resurrect it."""
        ...

    async def search(
        self, query: RecallQuery, space: EmbeddingSpace, vector: tuple[float, ...], *, limit: int
    ) -> list[SemanticHit]:
        """Exact original-source search with hard predicates before the bounded limit.

        Dated queries use separately indexed historical revisions, selected before
        similarity. Current vectors never stand in for historical evidence.
        """
        ...

    async def bounds(
        self, principal: Principal, *, include_history: bool = False
    ) -> SemanticBounds: ...

    async def inventory(
        self,
        principal: Principal,
        *,
        after: int,
        through: int,
        limit: int = 64,
        include_history: bool = False,
    ) -> SemanticPage:
        """Finite creation or historical change sweep; deleted/ineligible rows advance it."""
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


class RuntimeSemanticQueries(Protocol):
    async def validate_authority(self, *, session_id: UUID, run_id: UUID, surface_id: str) -> bool:
        """Revalidate saved evidence's read authority; never revive query work."""
        ...

    async def bind(
        self, *, session_id: UUID, run_id: UUID, surface_id: str
    ) -> SemanticQueryLookup | None:
        """Resolve a local cache against current trusted runtime state; no provider I/O."""
        ...

    async def prepare(
        self, queries: Sequence[RecallQuery], *, session_id: UUID, run_id: UUID, surface_id: str
    ) -> int:
        """Explicit bounded slow path, called outside repository transactions."""
        ...
