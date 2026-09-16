"""Previewed People erasure with owner fencing and content-free receipts."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.application.authorization import require_scope
from agent_core.application.people_erasure_batches import (
    ErasureManifest,
    batch_id,
    read_manifest,
    write_manifest,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.people import (
    PeopleCommitment,
    PeopleErasure,
    PeopleQuery,
    PeopleRecord,
    PeopleSource,
    Person,
    PersonMemoryLink,
    RelationshipAssertion,
)
from agent_core.domain.people_sources import source_id as event_source_id
from agent_core.domain.people_views import PeopleErasureView, PeopleForgetRequest
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class PeopleErasureService:
    def __init__(self, factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._factory = factory
        self._clock = clock
        self._cleanup: Callable[[], Awaitable[None]] | None = None

    async def _affected(
        self, uow: RepositoryUnitOfWork, principal: Principal, person_id: UUID, ceiling: Sensitivity
    ) -> list[PeopleRecord]:
        person = await uow.people.get(principal, person_id, ceiling=ceiling)
        if not isinstance(person, Person):
            raise NotFoundError("person not found")
        rows: dict[UUID, PeopleRecord] = {person.id: person}
        pending = [person.id]
        while pending:
            current = pending.pop()
            query = PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                person_id=current,
                include_superseded=True,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                limit=100,
                kinds=[
                    "person",
                    "identifier",
                    "mention",
                    "memory_link",
                    "relationship",
                    "interaction",
                    "commitment",
                    "operation",
                ],
            )
            while True:
                page = await uow.people.query(query)
                if ceiling != Sensitivity.RESTRICTED:
                    permitted = await uow.people.query(
                        query.model_copy(update={"sensitivity_ceiling": ceiling})
                    )
                    if not {row.id for row in page[:100]} <= {row.id for row in permitted}:
                        raise NotFoundError("person not found")
                for row in page[:100]:
                    if row.id in rows:
                        continue
                    rows[row.id] = row
                    if isinstance(row, Person):
                        pending.append(row.id)
                if len(page) <= 100:
                    break
                query = query.model_copy(update={"after": page[99].id})
        return list(rows.values())

    async def forget(
        self,
        principal: Principal,
        person_id: UUID,
        request: PeopleForgetRequest,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleErasureView:
        require_scope(principal, "people.write")
        if not key or len(key) > 200:
            raise ToolValidationError("People idempotency key is invalid")
        request = PeopleForgetRequest.model_validate(request.model_dump())
        digest = hashlib.sha256(
            json.dumps([str(person_id), request.model_dump(mode="json")], sort_keys=True).encode()
        ).hexdigest()
        key_hash = hashlib.sha256(
            json.dumps([principal.tenant_id, principal.principal_id, key]).encode()
        ).hexdigest()
        derivation = "people-erasure:" + key_hash
        async with self._factory() as uow, uow.email.lock(principal), uow.people.lock(principal):
            await uow.sessions.get(request.session_id, principal)
            replay = await uow.events.get_by_derivation(derivation, principal)
            if replay is not None:
                if replay.payload.get("request_hash") != digest:
                    raise ConflictError("People idempotency key was reused")
                receipt = await uow.people.get(
                    principal,
                    UUID(replay.payload["receipt_id"]),
                    ceiling=ceiling,
                )
                if not isinstance(receipt, PeopleErasure):
                    raise NotFoundError("People erasure receipt not found")
                return self.view(receipt)
            if request.phase == "preview":
                if request.operation_id is not None:
                    raise ToolValidationError("forget preview cannot name an operation")
                rows = await self._affected(uow, principal, person_id, ceiling)
                person = next(r for r in rows if r.id == person_id)
                if person.revision != request.expected_revision:
                    raise ConflictError("person revision changed")
                positions = {}
                for row in rows:
                    if isinstance(row, (PersonMemoryLink, RelationshipAssertion, PeopleCommitment)):
                        try:
                            belief = await uow.memories.get(row.belief_id, principal)
                        except NotFoundError:
                            continue
                        if SENSITIVITY_ORDER[belief.sensitivity] > SENSITIVITY_ORDER[ceiling]:
                            raise NotFoundError("person not found")
                        positions[belief.id] = belief.store_position
                source_sessions = set()
                blocked_sources = {key for row in rows for key in row.support_ids}
                for source_id in list(blocked_sources):
                    source = await uow.people.get(principal, source_id, ceiling=ceiling)
                    if isinstance(source, PeopleSource):
                        source_sessions.add(source.session_id)
                        blocked_sources.add(
                            event_source_id(principal, source.session_id, source.event_sequence)
                        )
                counts: dict[str, int] = dict(Counter(str(row.kind) for row in rows))
                counts["beliefs"] = len(positions)
                receipt = PeopleErasure(
                    id=uuid5(NAMESPACE_URL, derivation),
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    created_at=self._clock.now(),
                    updated_at=self._clock.now(),
                    sensitivity=ceiling,
                    target_id=person_id,
                    expected_revisions={},
                    counts=counts,
                    request_hash=digest,
                    expires_at=self._clock.now() + timedelta(minutes=10),
                )
                receipt = await write_manifest(
                    uow.people,
                    receipt,
                    ErasureManifest(
                        expected_revisions={r.id: r.revision for r in rows},
                        belief_positions=positions,
                        source_sessions=sorted(source_sessions),
                        blocked_source_ids=sorted(blocked_sources),
                    ),
                    now=self._clock.now(),
                    expected_revision=0,
                )
            else:
                if request.operation_id is None:
                    raise ToolValidationError("forget requires an exact preview")
                old = await uow.people.get(principal, request.operation_id, ceiling=ceiling)
                if (
                    not isinstance(old, PeopleErasure)
                    or old.target_id != person_id
                    or old.parent_id is not None
                ):
                    raise NotFoundError("People erasure receipt not found")
                if (
                    old.state != "preview"
                    or old.revision != request.expected_revision
                    or old.expires_at <= self._clock.now()
                ):
                    raise ConflictError("People erasure preview expired or changed")
                manifest = await read_manifest(uow.people, principal, old)
                rows = await self._affected(uow, principal, person_id, ceiling)
                if {r.id: r.revision for r in rows} != manifest.expected_revisions:
                    raise ConflictError("People changed; preview erasure again")
                for belief_id, expected_position in manifest.belief_positions.items():
                    belief = await uow.memories.get(belief_id, principal)
                    if (
                        belief.store_position != expected_position
                        or SENSITIVITY_ORDER[belief.sensitivity] > SENSITIVITY_ORDER[ceiling]
                    ):
                        raise ConflictError("People facts changed; preview erasure again")
                receipt = await self._apply_manifest(uow, principal, old, manifest)
            await uow.events.append(
                NewEvent(
                    session_id=request.session_id,
                    run_id=None,
                    event_type="people.erasure_receipt",
                    actor_type="user",
                    actor_id=principal.principal_id,
                    derivation_key=derivation,
                    payload={
                        "request_hash": digest,
                        "receipt_id": str(receipt.id),
                        "revision": receipt.revision,
                    },
                )
            )
            return self.view(receipt)

    async def export(
        self,
        principal: Principal,
        receipt_id: UUID,
    ) -> tuple[PeopleErasure, list[PeopleErasure]]:
        """Read a consistent, complete applied receipt for operator recovery."""
        from agent_core.application.people_erasure_restore import replay_manifest

        require_scope(principal, "people.read")
        async with self._factory() as uow, uow.people.lock(principal):
            root = await uow.people.get(principal, receipt_id, ceiling=Sensitivity.RESTRICTED)
            if not isinstance(root, PeopleErasure):
                raise NotFoundError("People erasure receipt not found")
            parts = []
            for index in range(root.batch_count):
                part = await uow.people.get(
                    principal,
                    batch_id(root.id, index),
                    ceiling=Sensitivity.RESTRICTED,
                )
                if not isinstance(part, PeopleErasure):
                    raise ConflictError("People erasure receipt page is missing")
                parts.append(part)
            replay_manifest(principal, root, parts)
            return root, parts

    async def reapply(
        self,
        principal: Principal,
        receipt: PeopleErasure,
        parts: list[PeopleErasure],
        *,
        session_id: UUID,
    ) -> PeopleErasureView:
        """Restore-only replay of a verified, newer content-free erasure receipt."""
        from agent_core.application.people_erasure_restore import reapply_erasure

        return await reapply_erasure(self, principal, receipt, parts, session_id=session_id)

    async def _apply_manifest(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        old: PeopleErasure,
        manifest: ErasureManifest,
    ) -> PeopleErasure:
        email_copies = await uow.email.fence_people_erasure(
            principal, list(manifest.belief_positions), self._clock.now()
        )
        pending_email = await uow.email.purge_people_erasure(principal)
        cleanup = await uow.session_deletions.erase_people_copies(
            principal,
            [*manifest.expected_revisions, *manifest.belief_positions],
            self._clock.now(),
        )
        belief_ids = sorted(manifest.belief_positions)
        await uow.memories.fence_for_erasure(principal, belief_ids)
        await uow.memories.purge_erased(principal, belief_ids[:256], operation_id=old.id)
        pending_beliefs = belief_ids[256:]
        episode_count = await uow.episodes.fence_for_erasure(principal, manifest.source_sessions)
        pending_episodes = await uow.episodes.purge_erased(principal)
        await uow.people.fence_for_erasure(principal, list(manifest.expected_revisions))
        pending_people = await uow.people.purge_erased(principal)
        receipt = old.model_copy(
            update={
                "state": "cleanup_pending"
                if pending_people
                or cleanup.pending_generated
                or pending_email
                or pending_episodes
                or pending_beliefs
                or cleanup.artifact_ids
                or cleanup.active_run_ids
                else "completed",
                "pending_people": pending_people,
                "pending_generated": cleanup.pending_generated,
                "pending_email": pending_email,
                "pending_episodes": pending_episodes,
                "counts": old.counts
                | cleanup.counts
                | {"email_copies": email_copies, "episodes": episode_count},
                "revision": old.revision + 1,
                "updated_at": max(self._clock.now(), old.updated_at + timedelta(microseconds=1)),
                "expected_revisions": {},
                "belief_positions": {},
                "source_sessions": [],
            }
        )
        receipt = await write_manifest(
            uow.people,
            receipt,
            ErasureManifest(
                pending_belief_ids=pending_beliefs,
                blocked_source_ids=manifest.blocked_source_ids,
                blocked_record_ids=sorted(
                    set(manifest.expected_revisions) | set(manifest.blocked_record_ids)
                ),
                blocked_belief_ids=sorted(
                    set(manifest.belief_positions) | set(manifest.blocked_belief_ids)
                ),
                pending_artifact_ids=cleanup.artifact_ids,
                pending_run_ids=cleanup.pending_run_ids,
            ),
            now=self._clock.now(),
            expected_revision=old.revision,
        )
        return receipt

    async def resume_pending(self, principal: Principal, *, limit: int = 20) -> int:
        """Internal maintenance continues fenced erasures even when People is disabled."""
        async with self._factory() as uow:
            page = await uow.people.query(
                PeopleQuery(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    kinds=["erasure"],
                    root_erasures_only=True,
                    states=["cleanup_pending"],
                    sensitivity_ceiling=Sensitivity.RESTRICTED,
                    limit=limit,
                )
            )
        count = 0
        for receipt in page[:limit]:
            if (
                isinstance(receipt, PeopleErasure)
                and receipt.state == "cleanup_pending"
                and receipt.parent_id is None
            ):
                await self._advance(principal, receipt.id, ceiling=Sensitivity.RESTRICTED)
                count += 1
        return count

    def set_cleanup(self, cleanup: Callable[[], Awaitable[None]]) -> None:
        self._cleanup = cleanup

    @staticmethod
    def view(receipt: PeopleErasure) -> PeopleErasureView:
        return PeopleErasureView(
            id=receipt.id, revision=receipt.revision, state=receipt.state, counts=receipt.counts
        )

    async def get(
        self, principal: Principal, receipt_id: UUID, *, ceiling: Sensitivity
    ) -> PeopleErasureView:
        require_scope(principal, "people.read")
        return await self._advance(principal, receipt_id, ceiling=ceiling)

    async def _advance(
        self, principal: Principal, receipt_id: UUID, *, ceiling: Sensitivity
    ) -> PeopleErasureView:
        async with self._factory() as uow:
            receipt = await uow.people.get(principal, receipt_id, ceiling=ceiling)
        if (
            isinstance(receipt, PeopleErasure)
            and receipt.parent_id is None
            and receipt.state == "cleanup_pending"
            and self._cleanup
        ):
            try:
                await self._cleanup()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "people_artifact_cleanup_pending", extra={"error_class": type(exc).__name__}
                )
        for _ in range(20):
            async with (
                self._factory() as uow,
                uow.email.lock(principal),
                uow.people.lock(principal),
            ):
                receipt = await uow.people.get(principal, receipt_id, ceiling=ceiling)
                if not isinstance(receipt, PeopleErasure) or receipt.parent_id is not None:
                    raise NotFoundError("People erasure receipt not found")
                if receipt.state == "preview":
                    await self._affected(uow, principal, receipt.target_id, ceiling)
                updated = await self._advance_part(uow, principal, receipt)
            # Commit each bounded page independently, including its durable cursor.
            if updated.state != "cleanup_pending" or updated == receipt:
                break
        return self.view(updated)

    async def _advance_part(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        receipt: PeopleErasure,
    ) -> PeopleErasure:
        if receipt.state != "cleanup_pending":
            return receipt
        if receipt.pending_people:
            receipt = receipt.model_copy(
                update={"pending_people": await uow.people.purge_erased(principal)}
            )
        if receipt.pending_generated:
            cleanup = await uow.session_deletions.erase_people_copies(
                principal,
                [],
                self._clock.now(),
            )
            receipt = receipt.model_copy(update={"pending_generated": cleanup.pending_generated})
        if receipt.pending_email:
            receipt = receipt.model_copy(
                update={"pending_email": await uow.email.purge_people_erasure(principal)}
            )
        if receipt.pending_episodes:
            receipt = receipt.model_copy(
                update={"pending_episodes": await uow.episodes.purge_erased(principal)}
            )
        if receipt.batch_count:
            remaining = receipt.cleanup_remaining
            cursor = receipt.cleanup_after_batch
            # One manifest page per transaction bounds belief and artifact work.
            part = await uow.people.get(
                principal,
                batch_id(receipt.id, cursor),
                ceiling=Sensitivity.RESTRICTED,
            )
            if not isinstance(part, PeopleErasure) or part.parent_id != receipt.id:
                raise ConflictError("People erasure manifest is incomplete")
            if part.state == "cleanup_pending":
                updated_part = await self._advance_part(uow, principal, part)
                remaining -= int(updated_part.state == "completed")
            cursor = (cursor + 1) % receipt.batch_count
            updated = receipt.model_copy(
                update={
                    "revision": receipt.revision + 1,
                    "updated_at": max(
                        self._clock.now(), receipt.updated_at + timedelta(microseconds=1)
                    ),
                    "cleanup_remaining": remaining,
                    "cleanup_after_batch": cursor,
                    "state": "cleanup_pending"
                    if remaining
                    or receipt.pending_people
                    or receipt.pending_generated
                    or receipt.pending_email
                    or receipt.pending_episodes
                    else "completed",
                    "counts": receipt.counts
                    | {"pending_batches": remaining}
                    | ({"pending_artifacts": 0, "active_runs": 0} if not remaining else {}),
                }
            )
            await uow.people.put(updated, expected_revision=receipt.revision)
            return updated
        if receipt.state == "cleanup_pending":
            pending_beliefs = list(receipt.pending_belief_ids)
            if pending_beliefs:
                await uow.memories.purge_erased(
                    principal, pending_beliefs, operation_id=receipt.parent_id or receipt.id
                )
                pending_beliefs = []
            # Initial cleanup fences every discovered copy and persists
            # all affected runs. Continue those run pages without rediscovering
            # overlapping runs from each record page.
            pending_copies: list[UUID] = []
            pending_runs = list(receipt.pending_run_ids)
            pending_artifacts = list(receipt.pending_artifact_ids)
            counts = dict(receipt.counts)
            pending = []
            for artifact_id in pending_artifacts:
                try:
                    await uow.artifacts.get(artifact_id, principal)
                except NotFoundError:
                    try:
                        await uow.trajectory_exports.get_artifact(artifact_id, principal)
                    except NotFoundError:
                        continue
                pending.append(artifact_id)
            # Drain this durable byte-cleanup page before admitting another.
            # Late outputs must never grow a child into nested manifest pages,
            # which would lose restore suppression when exporting the flat receipt.
            if not pending and (pending_copies or pending_runs):
                cleanup = await uow.session_deletions.erase_people_copies(
                    principal,
                    pending_copies,
                    self._clock.now(),
                    run_ids=pending_runs,
                    purge_generated=False,
                )
                counts.update(cleanup.counts)
                counts["active_runs"] = len(cleanup.active_run_ids)
                if not cleanup.active_run_ids:
                    pending_copies = []
                pending_runs = cleanup.pending_run_ids
                pending = cleanup.artifact_ids
            if (
                pending_beliefs != receipt.pending_belief_ids
                or receipt.pending_people
                or receipt.pending_generated
                or receipt.pending_email
                or receipt.pending_episodes
                or pending != receipt.pending_artifact_ids
                or pending_copies != receipt.pending_copy_ids
                or pending_runs != receipt.pending_run_ids
                or counts != receipt.counts
                or (not pending and not pending_copies and not pending_runs and not pending_beliefs)
            ):
                updated = receipt.model_copy(
                    update={
                        "revision": receipt.revision + 1,
                        "updated_at": max(
                            self._clock.now(), receipt.updated_at + timedelta(microseconds=1)
                        ),
                        "state": "cleanup_pending"
                        if receipt.pending_people
                        or receipt.pending_generated
                        or receipt.pending_email
                        or receipt.pending_episodes
                        or pending_beliefs
                        or pending
                        or pending_copies
                        or pending_runs
                        else "completed",
                        "pending_copy_ids": pending_copies,
                        "pending_run_ids": pending_runs,
                        "pending_artifact_ids": pending,
                        "counts": counts | {"pending_artifacts": len(pending)},
                    }
                )
                receipt = await write_manifest(
                    uow.people,
                    updated,
                    ErasureManifest(
                        pending_belief_ids=pending_beliefs,
                        blocked_source_ids=receipt.blocked_source_ids,
                        blocked_record_ids=receipt.blocked_record_ids,
                        blocked_belief_ids=receipt.blocked_belief_ids,
                        pending_copy_ids=pending_copies,
                        pending_run_ids=pending_runs,
                        pending_artifact_ids=pending,
                    ),
                    now=self._clock.now(),
                    expected_revision=receipt.revision,
                )
        return receipt
