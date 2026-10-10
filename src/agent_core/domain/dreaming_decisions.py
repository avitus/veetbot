"""An owner decision binds one stored operation and its revision; never new text."""

from datetime import datetime
from typing import Literal

from agent_core.domain.dreaming import OwnerDreamingReview
from agent_core.domain.errors import ConflictError
from agent_core.domain.reconsolidation_operations import StoredOperation, StoredSummary


def decide_review(
    value: StoredOperation,
    *,
    expected_revision: int,
    decision: Literal["approved", "rejected"],
    digest: str,
    now: datetime,
    source_valid: bool,
    position: int,
) -> StoredOperation:
    prior = value.owner_review
    if prior is None:
        raise ConflictError("operation is not an owner-review proposal")
    if prior.decision != "pending":
        if (prior.decision, prior.idempotency_digest, prior.expected_revision) == (
            decision,
            digest,
            expected_revision,
        ):
            return value
        raise ConflictError("proposal already has an owner decision")
    if value.revision != expected_revision or value.state != "committed" or not source_valid:
        raise ConflictError("proposal sources or revision changed; refresh before deciding")
    if now < value.created_at:
        raise ConflictError("decision cannot precede proposal")
    updated = value.model_copy(
        update={
            "owner_review": OwnerDreamingReview(
                decision=decision,
                decided_at=now,
                expected_revision=expected_revision,
                idempotency_digest=digest,
            ),
            "revision": value.revision + 1,
            "store_position": position,
        }
    )
    if isinstance(updated, StoredSummary):
        updated = updated.model_copy(update={"owner_revision": updated.owner_revision + 1})
    if decision == "rejected":
        updated = updated.model_copy(
            update={
                "state": "invalidated",
                "invalidated_at": now,
                "reason": "owner_undo"
                if value.kind == "merge"
                else "source_changed"
                if value.kind == "conflict"
                else "owner_rejection",
            }
        )
    return updated
