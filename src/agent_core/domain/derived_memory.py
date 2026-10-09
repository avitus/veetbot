"""Owner controls and explicit many-source public projections for summaries."""

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime

from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryBrowseQuery,
    MemoryStatus,
    Portability,
    Sensitivity,
    lexical_query_terms,
    lexical_term_lexemes,
    lexical_text_matches,
)
from agent_core.domain.reconsolidation import ReconValue
from agent_core.domain.reconsolidation_operations import StoredSummary
from agent_core.domain.reconsolidation_summary import PreparedSummary, SummaryClause, SummaryMemory
from agent_core.domain.reconsolidation_views import OperationSource

SummaryAction = Literal["dismiss", "not_here", "untrue", "delete"]


class SummaryWriteReceipt(ReconValue):
    operation_id: UUID
    request_hash: str


class DerivedMemoryContent(ReconValue):
    subject: str
    statement: str
    clauses: tuple[SummaryClause, ...]
    belief_types: tuple[BeliefType, ...]
    scope: str
    portability: Portability
    sensitivity: Sensitivity
    authority: MemoryAuthority
    confidence: float
    last_evidence_at: AwareDatetime
    valid_from: AwareDatetime
    expires_at: AwareDatetime | None

    @classmethod
    def from_summary(cls, summary: SummaryMemory) -> "DerivedMemoryContent":
        content = summary.content
        return cls(
            subject=content.subject,
            statement=content.rendered,
            clauses=content.clauses,
            belief_types=content.belief_types,
            scope=content.scope,
            portability=content.portability,
            sensitivity=content.sensitivity,
            authority=content.authority,
            confidence=content.confidence,
            last_evidence_at=content.last_evidence_at,
            valid_from=content.valid_from,
            expires_at=content.expires_at,
        )


class DerivedMemoryView(ReconValue):
    id: UUID
    record_kind: Literal["summary", "hypothesis"] = "summary"
    operation_id: UUID
    revision: int
    status: Literal["active", "retired"]
    flagged_for_review: bool
    created_at: AwareDatetime
    updated_at: AwareDatetime
    content: DerivedMemoryContent | None = None
    sources: tuple[OperationSource, ...] = ()


def summary_blocks(content: PreparedSummary) -> tuple[str, str]:
    """No prose survives rejection; copies, case, ordering and policy cannot evade it."""
    dependencies = {d.source.belief_id: d for d in content.plan.dependencies}
    claims = sorted(
        {
            (
                " ".join(clause.text.casefold().split()),
                tuple(sorted({dependencies[key].attribution_digest for key in clause.source_ids})),
            )
            for clause in content.clauses
        }
    )
    leaves = sorted(
        {
            (str(dependencies[key].source_session_id), sequence)
            for clause in content.clauses
            for key in clause.source_ids
            for sequence in dependencies[key].source_event_ids
        }
    )

    def digest(value: object) -> str:
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()

    return digest(["summary-claim@1", claims]), digest(["summary-source@1", claims, leaves])


def controlled_summary(
    summary: SummaryMemory | None, operation: StoredSummary, current_scope: str
) -> SummaryMemory | None:
    if summary is None or operation.owner_removed is not None:
        return None
    if operation.local_only and summary.content.scope != current_scope:
        return None
    content = summary.content
    if operation.local_only:
        content = content.model_copy(update={"portability": Portability.LOCAL})
    return summary.model_copy(
        update={
            "content": content,
            "flagged_for_review": not operation.reviewed,
            "revision": operation.owner_revision,
        }
    )


def summary_matches(summary: SummaryMemory, query: MemoryBrowseQuery) -> bool:
    content = summary.content
    return (
        MemoryStatus.ACTIVE in query.statuses
        and (not query.belief_types or bool(set(query.belief_types) & set(content.belief_types)))
        and (query.subject is None or content.subject.lower() == query.subject.lower())
        and (
            query.session_id is None
            or any(d.source_session_id == query.session_id for d in content.plan.dependencies)
        )
        and (
            query.flagged_for_review is None
            or summary.flagged_for_review == query.flagged_for_review
        )
        and (
            not lexical_query_terms(query.text)
            or lexical_text_matches(
                lexical_term_lexemes(lexical_query_terms(query.text)),
                content.subject + " " + content.rendered,
            )
        )
    )


def change_summary(
    operation: StoredSummary, action: SummaryAction, now: datetime, position: int
) -> StoredSummary:
    removed = action in {"untrue", "delete"}
    return operation.model_copy(
        update={
            "revision": operation.revision + 1,
            "owner_revision": operation.revision + 1,
            "store_position": position,
            "updated_at": now,
            "reviewed": action == "dismiss" if not removed else False,
            "local_only": operation.local_only or action == "not_here",
            "owner_removed": action if removed else None,
            "state": "invalidated" if removed else "committed",
            "invalidated_at": now if removed else None,
            "reason": {
                "dismiss": "owner_reviewed",
                "not_here": "owner_not_here",
                "untrue": "owner_rejection",
                "delete": "owner_delete",
            }[action],
        }
    )
