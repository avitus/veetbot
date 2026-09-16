"""Reapply verified erasure receipts before an older restored database serves reads."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from agent_core.application.authorization import require_scope
from agent_core.application.people_erasure_batches import ErasureManifest, batch_id, write_manifest
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    PeopleCommitment,
    PeopleErasure,
    PeopleSource,
    Person,
    PersonMemoryLink,
    RelationshipAssertion,
)
from agent_core.domain.people_sources import source_id as event_source_id
from agent_core.domain.people_views import PeopleErasureView

if TYPE_CHECKING:
    from agent_core.application.people_erasure import PeopleErasureService


def replay_manifest(
    principal: Principal, root: PeopleErasure, parts: list[PeopleErasure]
) -> ErasureManifest:
    root = PeopleErasure.model_validate(root.model_dump())
    parts = [PeopleErasure.model_validate(part.model_dump()) for part in parts]
    owner = (principal.tenant_id, principal.principal_id)
    if (
        root.parent_id is not None
        or root.state == "preview"
        or (root.tenant_id, root.principal_id) != owner
    ):
        raise ToolValidationError("restore requires an applied receipt for this exact owner")
    if len(parts) != root.batch_count:
        raise ToolValidationError("restore erasure receipt pages are incomplete")
    if any(
        part.id != batch_id(root.id, index)
        or part.parent_id != root.id
        or part.batch_count
        or part.state == "preview"
        or (part.tenant_id, part.principal_id) != owner
        or part.target_id != root.target_id
        or part.request_hash != root.request_hash
        for index, part in enumerate(parts)
    ):
        raise ToolValidationError("restore erasure receipt pages do not match their root")
    result = ErasureManifest()
    for part in parts or [root]:
        result.blocked_record_ids.extend(part.blocked_record_ids)
        result.blocked_belief_ids.extend(part.blocked_belief_ids)
        result.blocked_source_ids.extend(part.blocked_source_ids)
    if root.target_id not in result.blocked_record_ids:
        raise ToolValidationError("receipt lacks the opaque identities required for restore")
    return result


async def reapply_erasure(
    service: PeopleErasureService,
    principal: Principal,
    root: PeopleErasure,
    parts: list[PeopleErasure],
    *,
    session_id: UUID,
) -> PeopleErasureView:
    require_scope(principal, "people.read")
    require_scope(principal, "people.write")
    manifest = replay_manifest(principal, root, parts)
    digest = hashlib.sha256(
        json.dumps(
            [
                str(root.id),
                str(root.target_id),
                {
                    key: sorted({str(item) for item in value})
                    for key, value in vars(manifest).items()
                },
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    operation_id = uuid5(root.id, "post-snapshot-erasure-replay@1")
    async with service._factory() as uow, uow.email.lock(principal), uow.people.lock(principal):
        await uow.sessions.get(session_id, principal)
        existing = await uow.people.get(principal, operation_id, ceiling=Sensitivity.RESTRICTED)
        if existing is not None:
            if not isinstance(existing, PeopleErasure) or existing.request_hash != digest:
                raise ConflictError("restore erasure receipt differs from the prior replay")
            return service.view(existing)
        restored = {}
        for record_id in sorted(set(manifest.blocked_record_ids)):
            row = await uow.people.get(principal, record_id, ceiling=Sensitivity.RESTRICTED)
            if isinstance(row, Person):
                restored.update(
                    {
                        item.id: item
                        for item in await service._affected(
                            uow, principal, row.id, Sensitivity.RESTRICTED
                        )
                    }
                )
            elif row is not None:
                restored[row.id] = row
        manifest.expected_revisions = {
            record_id: restored[record_id].revision if record_id in restored else 0
            for record_id in set(manifest.blocked_record_ids) | restored.keys()
        }
        belief_ids = set(manifest.blocked_belief_ids) | {
            row.belief_id
            for row in restored.values()
            if isinstance(row, (PersonMemoryLink, RelationshipAssertion, PeopleCommitment))
        }
        for belief_id in sorted(belief_ids):
            try:
                belief = await uow.memories.get(belief_id, principal)
            except NotFoundError:
                continue
            manifest.belief_positions[belief_id] = belief.store_position
        source_ids = set(manifest.blocked_source_ids) | {
            source for row in restored.values() for source in row.support_ids
        }
        manifest.blocked_source_ids = sorted(source_ids)
        for source_id in sorted(source_ids):
            source = await uow.people.get(principal, source_id, ceiling=Sensitivity.RESTRICTED)
            if isinstance(source, PeopleSource):
                manifest.source_sessions.append(source.session_id)
                manifest.blocked_source_ids.append(
                    event_source_id(principal, source.session_id, source.event_sequence)
                )
        manifest.blocked_source_ids = sorted(set(manifest.blocked_source_ids))
        old = PeopleErasure(
            id=operation_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            target_id=root.target_id,
            created_at=service._clock.now(),
            updated_at=service._clock.now(),
            sensitivity=Sensitivity.RESTRICTED,
            expires_at=service._clock.now() + timedelta(minutes=10),
            request_hash=digest,
            expected_revisions={},
            counts={
                "records": len(manifest.expected_revisions),
                "beliefs": len(manifest.belief_positions),
            },
        )
        old = await write_manifest(
            uow.people, old, manifest, now=service._clock.now(), expected_revision=0
        )
        receipt = await service._apply_manifest(uow, principal, old, manifest)
        await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type="people.erasure_receipt",
                actor_type="user",
                actor_id=principal.principal_id,
                derivation_key=f"people-restore-erasure:{principal.tenant_id}:{principal.principal_id}:{operation_id}",
                payload={
                    "receipt_id": str(receipt.id),
                    "revision": receipt.revision,
                    "request_hash": digest,
                    "restored_receipt_id": str(root.id),
                },
            )
        )
        return service.view(receipt)
