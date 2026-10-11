"""Allow-listed owner inspection values; no internal plans, digests or counters."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.errors import AgentCoreError
from agent_core.domain.reconsolidation import ReconValue
from agent_core.domain.reconsolidation_summary import SummaryClause

OperationKind = Literal["merge", "summary", "hypothesis", "conflict"]
OperationState = Literal["proposed", "committed", "rejected", "stale", "invalidated", "undone"]


class OperationCursor(ReconValue):
    created_at: AwareDatetime
    id: UUID


class OperationSource(ReconValue):
    belief_id: UUID
    content_revision: int = Field(ge=1)
    subject: str
    statement: str
    session_id: UUID
    event_ids: tuple[int, ...]
    omitted: bool = False


class OperationContent(ReconValue):
    memory_id: UUID
    subject: str
    statement: str
    clauses: tuple[SummaryClause, ...] = ()


class OperationView(ReconValue):
    id: UUID
    kind: OperationKind
    state: OperationState
    revision: int = Field(ge=1)
    reason: Literal[
        "equivalent",
        "summarized",
        "inferred",
        "conflict_flagged",
        "source_changed",
        "owner_undo",
        "owner_reviewed",
        "owner_not_here",
        "owner_rejection",
        "owner_delete",
    ]
    policy: str
    model_identity: str
    created_at: AwareDatetime
    committed_at: AwareDatetime
    invalidated_at: AwareDatetime | None = None
    undone_at: AwareDatetime | None = None
    content: OperationContent | None = None
    sources: tuple[OperationSource, ...] = ()


class UndoOperationRequest(ReconValue):
    expected_revision: int = Field(strict=True, ge=1)


class ReconsolidationValidationError(AgentCoreError):
    """Invalid owner-control parameters, mapped to the M32 validation vocabulary."""
