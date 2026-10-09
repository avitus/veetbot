"""Many-source memory controls share the ordinary owner's transaction and fences."""

import hashlib
from datetime import datetime
from uuid import UUID, uuid5

from agent_core.application.people_erasure_batches import ErasureManifest, write_manifest
from agent_core.domain.agents import Principal
from agent_core.domain.derived_memory import DerivedMemoryContent, DerivedMemoryView, SummaryAction
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleErasure
from agent_core.ports.persistence import RepositoryUnitOfWork


async def derived_view(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    memory_id: UUID,
    ceiling: Sensitivity,
    now: datetime,
) -> DerivedMemoryView:
    operation = await uow.reconsolidation.summary_operation(principal, memory_id)
    if operation is None or operation.owner_removed == "delete":
        raise NotFoundError("memory not found")
    visible = await uow.reconsolidation.get_operation(principal, memory_id, now, ceiling=ceiling)
    if visible is None:
        raise NotFoundError("memory not found")
    operation = await uow.reconsolidation.summary_operation(principal, memory_id)
    assert operation is not None
    content = None
    if operation.owner_removed != "untrue":
        if visible.content is None or not visible.sources:
            raise NotFoundError("memory not found")
        original = await uow.memories.get(visible.sources[0].belief_id, principal)
        summary = await uow.reconsolidation.get_summary(
            principal, memory_id, now, ceiling=ceiling, current_scope=original.scope
        )
        if summary is None:
            raise NotFoundError("memory not found")
        content = DerivedMemoryContent.from_summary(summary)
    return DerivedMemoryView(
        id=memory_id,
        record_kind=operation.kind,
        operation_id=memory_id,
        revision=operation.revision,
        status="retired" if operation.owner_removed == "untrue" else "active",
        flagged_for_review=not operation.reviewed,
        created_at=operation.created_at,
        updated_at=operation.updated_at or operation.created_at,
        content=content,
        sources=visible.sources if content is not None else (),
    )


async def change_derived(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    memory_id: UUID,
    action: SummaryAction,
    ceiling: Sensitivity,
    now: datetime,
) -> None:
    visible = await derived_view(uow, principal, memory_id, ceiling, now)
    targets = (
        await uow.reconsolidation.summary_rejection_targets(principal, memory_id)
        if action in {"delete", "untrue"}
        else ()
    )
    await uow.reconsolidation.change_summary(principal, memory_id, action, now)
    if action not in {"delete", "untrue"}:
        return
    cleanup = await uow.session_deletions.erase_people_copies(principal, list(targets), now)
    receipt = PeopleErasure(
        id=uuid5(memory_id, "derived-memory-erasure@1"),
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        target_id=memory_id,
        created_at=now,
        updated_at=now,
        expires_at=now,
        sensitivity=visible.content.sensitivity
        if visible.content is not None
        else Sensitivity.RESTRICTED,
        expected_revisions={},
        request_hash=hashlib.sha256(
            f"{principal.tenant_id}/{principal.principal_id}/{memory_id}/erase-derived@1".encode()
        ).hexdigest(),
        pending_generated=cleanup.pending_generated,
        counts=cleanup.counts | {"derived_memories": 1},
        state="cleanup_pending"
        if cleanup.pending_generated or cleanup.artifact_ids or cleanup.pending_run_ids
        else "completed",
    )
    # A later Delete of an already rejected summary shares the existing erasure
    # fence. Its durable cleanup manifest must not lose outstanding work.
    existing = await uow.people.get(principal, receipt.id, ceiling=Sensitivity.RESTRICTED)
    if existing is None:
        await write_manifest(
            uow.people,
            receipt,
            ErasureManifest(
                blocked_record_ids=list(targets),
                blocked_belief_ids=list(targets),
                pending_artifact_ids=cleanup.artifact_ids,
                pending_run_ids=cleanup.pending_run_ids,
            ),
            now=now,
            expected_revision=0,
        )
