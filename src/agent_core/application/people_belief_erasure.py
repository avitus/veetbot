"""Remove one belief's derived influence without forgetting its people or sources."""

import hashlib
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid5

from agent_core.application.people_erasure_batches import ErasureManifest, write_manifest
from agent_core.domain.agents import Principal
from agent_core.domain.memory import MemoryRecord, Sensitivity
from agent_core.domain.people import PeopleErasure, PeopleQuery
from agent_core.ports.persistence import RepositoryUnitOfWork


def belief_erasure_id(belief_id: UUID) -> UUID:
    return uuid5(belief_id, "belief-erasure@1")


async def withhold_email_summaries(
    uow: RepositoryUnitOfWork, principal: Principal, belief_ids: Sequence[UUID]
) -> int:
    """Withhold the generated summary of every exchange whose message formed a belief.

    A summary may restate the deleted fact, as the thread summary this deletion
    also resets may (ADR-0117, ADR-0126). Call before the email copies are fenced.
    """
    threads: dict[tuple[str, str], set[str]] = {}
    for account_id, thread_id, message_id in await uow.email.belief_messages(principal, belief_ids):
        threads.setdefault((account_id, thread_id), set()).add(message_id)
    interactions: set[UUID] = set()
    for (account_id, thread_id), message_ids in sorted(threads.items()):
        sources = await _all(
            uow,
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kinds=["source"],
                account_id=account_id,
                thread_id=thread_id,
                message_ids=sorted(message_ids),
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                limit=100,
            ),
        )
        for source_id in sources:
            interactions.update(
                await _all(
                    uow,
                    PeopleQuery(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        kinds=["interaction"],
                        source_id=source_id,
                        include_superseded=True,
                        sensitivity_ceiling=Sensitivity.RESTRICTED,
                        limit=100,
                    ),
                )
            )
    if not interactions:
        return 0
    return await uow.people.withhold_generated_summaries(principal, sorted(interactions))


async def _all(uow: RepositoryUnitOfWork, query: PeopleQuery) -> list[UUID]:
    found: list[UUID] = []
    while True:
        page = await uow.people.query(query)
        found.extend(row.id for row in page[:100])
        if len(page) <= 100:
            return found
        query = query.model_copy(update={"after": page[99].id})


async def erase_belief_copies(
    uow: RepositoryUnitOfWork, principal: Principal, belief: MemoryRecord, now: datetime
) -> PeopleErasure:
    """Caller holds the owner fence and deletes the belief in the same transaction."""
    query = PeopleQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        kinds=["memory_link", "relationship", "commitment"],
        belief_id=belief.id,
        include_superseded=True,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=100,
    )
    links: list[UUID] = []
    while True:
        page = await uow.people.query(query)
        links.extend(row.id for row in page[:100])
        if len(page) <= 100:
            break
        query = query.model_copy(update={"after": page[99].id})
    cleanup = await uow.session_deletions.erase_people_copies(principal, [belief.id, *links], now)
    await uow.people.fence_for_erasure(principal, links)
    pending_people = await uow.people.purge_erased(principal)
    await withhold_email_summaries(uow, principal, [belief.id])
    email_copies = await uow.email.fence_people_erasure(principal, [belief.id], now)
    pending_email = await uow.email.purge_people_erasure(principal)
    episodes = await uow.episodes.fence_for_erasure(principal, [belief.source_session_id])
    pending_episodes = await uow.episodes.purge_erased(principal)
    receipt = PeopleErasure(
        id=belief_erasure_id(belief.id),
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        target_id=belief.id,
        created_at=now,
        updated_at=now,
        expires_at=now,
        sensitivity=belief.sensitivity,
        expected_revisions={},
        request_hash=hashlib.sha256(
            f"{principal.tenant_id}/{principal.principal_id}/{belief.id}/erase-belief@1".encode()
        ).hexdigest(),
        pending_people=pending_people,
        pending_generated=cleanup.pending_generated,
        pending_email=pending_email,
        pending_episodes=pending_episodes,
        counts=cleanup.counts | {"beliefs": 1, "email_copies": email_copies, "episodes": episodes},
        state="cleanup_pending"
        if pending_people
        or cleanup.pending_generated
        or pending_email
        or pending_episodes
        or cleanup.artifact_ids
        or cleanup.pending_run_ids
        else "completed",
    )
    return await write_manifest(
        uow.people,
        receipt,
        ErasureManifest(
            blocked_record_ids=[belief.id, *links],
            blocked_belief_ids=[belief.id],
            pending_artifact_ids=cleanup.artifact_ids,
            pending_run_ids=cleanup.pending_run_ids,
        ),
        now=now,
        expected_revision=0,
    )
