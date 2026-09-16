"""Content-free deletion evidence shared by bounded memory cleanup adapters."""

import hashlib
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid5

from agent_core.domain.memory import BeliefRejection, MemoryRecord, RejectionKind


def erased_email_payload(kind: str, payload: Mapping[str, object]) -> dict[str, object]:
    """Remove generated People copies while preserving original email evidence."""
    if kind == "semantic_source":
        return {**payload, "excluded": True, "memory_ids": [], "facts": {}}
    if kind == "thread":
        return {
            **payload,
            "summary": "Review the original conversation.",
            "reason": "Related People memory was erased.",
            "topics": [],
        }
    return {}


def memory_erasure_tombstone(
    record: MemoryRecord, operation_id: UUID, at: datetime
) -> BeliefRejection:
    return BeliefRejection(
        id=uuid5(operation_id, str(record.id)),
        tenant_id=record.tenant_id,
        principal_id=record.principal_id,
        belief_id=record.id,
        kind=RejectionKind.DELETED,
        subject="erased memory",
        statement=None,
        statement_sha256=hashlib.sha256(record.statement.casefold().encode()).hexdigest(),
        belief_type=record.belief_type,
        scope=record.scope,
        created_at=at,
    )


def erased_rejection(rejection: BeliefRejection) -> BeliefRejection:
    """Retain suppression hashes, never the deleted assertion or subject text."""
    return rejection.model_copy(
        update={
            "kind": RejectionKind.DELETED,
            "subject": "erased memory",
            "statement": None,
            "replacement_id": None,
            "trace_id": None,
        }
    )
