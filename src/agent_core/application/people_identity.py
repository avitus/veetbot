"""Owner-authorized, revision-bound identity repair."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.people import (
    PEOPLE_RECORD,
    IdentityAssignment,
    PeopleCommitment,
    PeopleInteraction,
    PeopleOperation,
    PeopleQuery,
    PeopleRecord,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    PersonMention,
    RelationshipAssertion,
)
from agent_core.domain.people_views import PeopleIdentityRequest
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.people import PeopleStore
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

ASSIGNABLE_KINDS = [
    "identifier",
    "mention",
    "memory_link",
    "relationship",
    "interaction",
    "commitment",
]


def reassign(record: PeopleRecord, before: UUID, after: UUID, now: datetime) -> PeopleRecord:
    """Rewrite typed assignments only; the atomic belief remains authoritative."""
    payload = record.model_dump()
    payload.update(
        revision=record.revision + 1,
        updated_at=max(now, record.updated_at + timedelta(microseconds=1)),
    )
    if isinstance(record, (PersonIdentifier, PersonMention, PersonMemoryLink)):
        if record.person_id != before:
            raise ConflictError("identity assignment changed")
        payload["person_id"] = after
    elif isinstance(record, PeopleInteraction):
        participants = [
            p.model_copy(update={"person_id": after}) if p.person_id == before else p
            for p in record.participants
        ]
        payload["participants"] = list(
            {(p.person_id, p.role): p.model_dump() for p in participants}.values()
        )
    elif isinstance(record, (RelationshipAssertion, PeopleCommitment)):
        fields = (
            ("subject", "object")
            if isinstance(record, RelationshipAssertion)
            else ("debtor", "beneficiary")
        )
        for field in fields:
            if payload[field]["kind"] == "person" and payload[field]["id"] == before:
                payload[field]["id"] = after
    else:
        raise ConflictError("record does not support identity assignment")
    try:
        return PEOPLE_RECORD.validate_python(payload)
    except ValueError as exc:
        raise ConflictError("identity repair requires resolving a dependent relationship") from exc


async def assignments_for(
    store: PeopleStore, principal: Principal, person_id: UUID, ceiling: Sensitivity
) -> list[PeopleRecord]:
    query = PeopleQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        kinds=ASSIGNABLE_KINDS,
        include_superseded=True,
        person_id=person_id,
        sensitivity_ceiling=ceiling,
        limit=100,
    )
    records: list[PeopleRecord] = []
    while True:
        page = await store.query(query)
        records.extend(page[:100])
        if len(page) <= 100:
            return records
        if len(records) >= 1000:
            raise ConflictError("identity operation exceeds the bounded repair limit")
        query = query.model_copy(update={"after": records[-1].id})


class PeopleIdentityService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock, ids: IdFactory) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids

    async def request(
        self,
        principal: Principal,
        request: PeopleIdentityRequest,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleOperation:
        require_scope(principal, "people.write")
        if not key or len(key) > 200:
            raise ToolValidationError("People idempotency key is invalid")
        request = PeopleIdentityRequest.model_validate(request.model_dump())
        digest = hashlib.sha256(
            json.dumps(request.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        receipt_key = (
            "people-identity:"
            + hashlib.sha256(
                json.dumps([principal.tenant_id, principal.principal_id, key]).encode()
            ).hexdigest()
        )
        async with self._transaction(principal) as uow:
            await uow.sessions.get(request.session_id, principal)
            previous = await uow.events.get_by_derivation(receipt_key, principal)
            if previous is not None:
                if previous.payload.get("request_hash") != digest:
                    raise ConflictError("People idempotency key was reused")
                result = await uow.people.get(
                    principal,
                    UUID(previous.payload["operation_id"]),
                    ceiling=ceiling,
                    at_revision=int(previous.payload["revision"]),
                )
                if not isinstance(result, PeopleOperation):
                    raise NotFoundError("identity operation not found")
                return result
            if request.operation in {"merge", "split"}:
                if (
                    request.source_id is None
                    or request.target_id is None
                    or request.operation_id is not None
                ):
                    raise ToolValidationError("identity preview requires source and target")
                if request.operation == "merge":
                    if request.selected_ids:
                        raise ToolValidationError("merge cannot select a subset")
                    result = await self.preview_merge(
                        principal,
                        request.source_id,
                        request.target_id,
                        expected_revisions=request.expected_revisions,
                        ceiling=ceiling,
                        existing_uow=uow,
                    )
                else:
                    result = await self.preview_split(
                        principal,
                        request.source_id,
                        request.target_id,
                        selected_ids=request.selected_ids,
                        expected_revisions=request.expected_revisions,
                        ceiling=ceiling,
                        existing_uow=uow,
                    )
            else:
                if (
                    request.operation_id is None
                    or request.source_id is not None
                    or request.target_id is not None
                    or request.selected_ids
                    or request.expected_revisions
                ):
                    raise ToolValidationError(
                        "identity operation requires only its preview identifier"
                    )
                current = await uow.people.get(principal, request.operation_id, ceiling=ceiling)
                if not isinstance(current, PeopleOperation):
                    raise NotFoundError("identity operation not found")
                if current.revision != request.expected_revision:
                    raise ConflictError("identity operation revision changed")
                result = (
                    await self.preview_undo(
                        principal, request.operation_id, ceiling=ceiling, existing_uow=uow
                    )
                    if request.operation == "undo"
                    else await self.apply(
                        principal, request.operation_id, ceiling=ceiling, existing_uow=uow
                    )
                )
            await uow.events.append(
                NewEvent(
                    session_id=request.session_id,
                    run_id=None,
                    event_type="people.identity_operation",
                    actor_type="user",
                    actor_id=principal.principal_id,
                    derivation_key=receipt_key,
                    payload={
                        "request_hash": digest,
                        "operation_id": str(result.id),
                        "revision": result.revision,
                    },
                )
            )
            return result

    @asynccontextmanager
    async def _transaction(
        self, principal: Principal, existing: RepositoryUnitOfWork | None = None
    ) -> AsyncIterator[RepositoryUnitOfWork]:
        if existing is not None:
            yield existing
        else:
            async with self._uow_factory() as uow, uow.people.lock(principal):
                yield uow

    async def preview_merge(
        self,
        principal: Principal,
        source_id: UUID,
        target_id: UUID,
        *,
        expected_revisions: dict[UUID, int],
        ceiling: Sensitivity,
        existing_uow: RepositoryUnitOfWork | None = None,
    ) -> PeopleOperation:
        require_scope(principal, "people.write")
        if source_id == target_id or set(expected_revisions) != {source_id, target_id}:
            raise ConflictError("identity repair requires two distinct current identities")
        async with self._transaction(principal, existing_uow) as uow:
            people = [
                await self._person(uow.people, principal, key, ceiling)
                for key in (source_id, target_id)
            ]
            if any(p.revision != expected_revisions[p.id] or p.state == "merged" for p in people):
                raise ConflictError("identity revision changed")
            # Hidden assignments must not silently survive a merge. Require a
            # ceiling that permits the entire affected set, without revealing it.
            rows = await assignments_for(uow.people, principal, source_id, Sensitivity.RESTRICTED)
            for row in rows:
                if await uow.people.get(principal, row.id, ceiling=ceiling) is None:
                    raise NotFoundError("identity not found")
                reassign(row, source_id, target_id, self._clock.now())
            operation = self._operation(
                principal,
                "merge",
                people,
                [
                    IdentityAssignment(
                        entity_id=r.id,
                        before_person_id=source_id,
                        after_person_id=target_id,
                        expected_revision=r.revision,
                        previous_participants=r.participants
                        if isinstance(r, PeopleInteraction)
                        else None,
                    )
                    for r in rows
                ],
            )
            await uow.people.put(operation, expected_revision=0)
            return operation

    async def preview_split(
        self,
        principal: Principal,
        source_id: UUID,
        target_id: UUID,
        *,
        selected_ids: list[UUID],
        expected_revisions: dict[UUID, int],
        ceiling: Sensitivity,
        existing_uow: RepositoryUnitOfWork | None = None,
    ) -> PeopleOperation:
        require_scope(principal, "people.write")
        if (
            source_id == target_id
            or set(expected_revisions) != {source_id, target_id}
            or not selected_ids
            or len(selected_ids) > 1000
            or len(selected_ids) != len(set(selected_ids))
        ):
            raise ConflictError("split requires distinct identities and selected evidence")
        async with self._transaction(principal, existing_uow) as uow:
            people = [
                await self._person(uow.people, principal, key, ceiling)
                for key in (source_id, target_id)
            ]
            if any(p.revision != expected_revisions[p.id] or p.state == "merged" for p in people):
                raise ConflictError("identity revision changed")
            rows = await assignments_for(uow.people, principal, source_id, Sensitivity.RESTRICTED)
            selected = [r for r in rows if r.id in selected_ids]
            if {r.id for r in selected} != set(selected_ids):
                raise NotFoundError("identity assignment not found")
            for row in selected:
                if await uow.people.get(principal, row.id, ceiling=ceiling) is None:
                    raise NotFoundError("identity assignment not found")
                reassign(row, source_id, target_id, self._clock.now())
            moved_sources = {key for r in selected for key in r.support_ids}
            kept_sources = {
                key
                for r in rows
                if r.id not in selected_ids and isinstance(r, (PersonMention, PersonIdentifier))
                for key in r.support_ids
            }
            mixed = [
                r
                for r in rows
                if isinstance(r, (PersonMemoryLink, RelationshipAssertion, PeopleCommitment))
                and set(r.support_ids) & moved_sources
                and set(r.support_ids) & kept_sources
            ]
            for row in mixed:
                if await uow.people.get(principal, row.id, ceiling=ceiling) is None:
                    raise NotFoundError("identity assignment not found")
            selected = [r for r in selected if r.id not in {r.id for r in mixed}]
            changes = [
                IdentityAssignment(
                    entity_id=r.id,
                    before_person_id=source_id,
                    after_person_id=target_id,
                    expected_revision=r.revision,
                    previous_participants=r.participants
                    if isinstance(r, PeopleInteraction)
                    else None,
                )
                for r in selected
            ]
            changes.extend(
                IdentityAssignment(
                    entity_id=r.id,
                    before_person_id=source_id,
                    after_person_id=source_id,
                    expected_revision=r.revision,
                    previous_unresolved=r.unresolved,
                    replacement_unresolved=True,
                )
                for r in mixed
            )
            operation = self._operation(principal, "split", people, changes).model_copy(
                update={
                    "assignment_scope_hash": await self._scope_hash(uow.people, principal, rows)
                }
            )
            await uow.people.put(operation, expected_revision=0)
            return operation

    async def preview_undo(
        self,
        principal: Principal,
        operation_id: UUID,
        *,
        ceiling: Sensitivity,
        existing_uow: RepositoryUnitOfWork | None = None,
    ) -> PeopleOperation:
        require_scope(principal, "people.write")
        async with self._transaction(principal, existing_uow) as uow:
            previous = await uow.people.get(principal, operation_id, ceiling=ceiling)
            if not isinstance(previous, PeopleOperation):
                raise NotFoundError("identity operation not found")
            if previous.state != "completed" or previous.operation not in {"merge", "split"}:
                raise ConflictError("identity operation cannot be undone")
            people = [
                await self._person(uow.people, principal, key, ceiling)
                for key in previous.person_ids
            ]
            if any(p.revision != previous.expected_revisions[p.id] + 1 for p in people):
                raise ConflictError("identity changed since repair")
            assignments = []
            for old in previous.assignments:
                current = await uow.people.get(principal, old.entity_id, ceiling=ceiling)
                if current is None or current.revision != old.expected_revision + 1:
                    raise ConflictError("identity assignment changed since repair")
                reassign(current, old.after_person_id, old.before_person_id, self._clock.now())
                assignments.append(
                    IdentityAssignment(
                        entity_id=old.entity_id,
                        before_person_id=old.after_person_id,
                        after_person_id=old.before_person_id,
                        expected_revision=current.revision,
                        replacement_participants=old.previous_participants,
                        replacement_unresolved=old.previous_unresolved,
                    )
                )
            operation = self._operation(principal, "undo", people, assignments).model_copy(
                update={
                    "undo_of": previous.id,
                    "original_states": previous.original_states,
                }
            )
            await uow.people.put(operation, expected_revision=0)
            return operation

    async def apply(
        self,
        principal: Principal,
        operation_id: UUID,
        *,
        ceiling: Sensitivity,
        existing_uow: RepositoryUnitOfWork | None = None,
    ) -> PeopleOperation:
        require_scope(principal, "people.write")
        async with self._transaction(principal, existing_uow) as uow:
            operation = await uow.people.get(principal, operation_id, ceiling=ceiling)
            if not isinstance(operation, PeopleOperation):
                raise NotFoundError("identity operation not found")
            if operation.state == "completed":
                return operation
            if operation.state != "preview" or operation.expires_at <= self._clock.now():
                raise ConflictError("identity preview expired")
            people = [
                await self._person(uow.people, principal, key, ceiling)
                for key in operation.person_ids
            ]
            if any(p.revision != operation.expected_revisions[p.id] for p in people):
                raise ConflictError("identity revision changed")
            if operation.operation == "split":
                current_rows = await assignments_for(
                    uow.people, principal, people[0].id, Sensitivity.RESTRICTED
                )
                if (
                    await self._scope_hash(uow.people, principal, current_rows)
                    != operation.assignment_scope_hash
                ):
                    raise ConflictError("identity assignments changed; preview again")
            if operation.operation == "merge":
                current_rows = await assignments_for(
                    uow.people, principal, people[0].id, Sensitivity.RESTRICTED
                )
                if {r.id: r.revision for r in current_rows} != {
                    a.entity_id: a.expected_revision for a in operation.assignments
                }:
                    raise ConflictError("identity assignments changed; preview again")
            writes: list[PeopleRecord] = []
            for assignment in operation.assignments:
                row = await uow.people.get(principal, assignment.entity_id, ceiling=ceiling)
                if row is None or row.revision != assignment.expected_revision:
                    raise ConflictError("identity assignment changed")
                rewritten = reassign(
                    row, assignment.before_person_id, assignment.after_person_id, self._clock.now()
                )
                if assignment.replacement_participants is not None:
                    rewritten = PEOPLE_RECORD.validate_python(
                        rewritten.model_dump()
                        | {
                            "participants": assignment.replacement_participants,
                        }
                    )
                if assignment.replacement_unresolved is not None:
                    rewritten = PEOPLE_RECORD.validate_python(
                        rewritten.model_dump()
                        | {
                            "unresolved": assignment.replacement_unresolved,
                        }
                    )
                writes.append(rewritten)
            person_writes = []
            for person in people:
                update: dict[str, object] = {
                    "revision": person.revision + 1,
                    "updated_at": max(
                        self._clock.now(), person.updated_at + timedelta(microseconds=1)
                    ),
                }
                if operation.operation == "merge" and person.id == people[0].id:
                    update.update(state="merged", merged_into=people[1].id)
                elif operation.operation == "undo":
                    update.update(state=operation.original_states[person.id], merged_into=None)
                person_writes.append(Person.model_validate(person.model_dump() | update))
            # Validate the complete write set before mutating deterministic storage.
            for row in writes:
                await uow.people.put(row, expected_revision=row.revision - 1)
            for person in person_writes:
                await uow.people.put(person, expected_revision=person.revision - 1)
            completed = operation.model_copy(
                update={
                    "state": "completed",
                    "revision": 2,
                    "updated_at": max(
                        self._clock.now(), operation.updated_at + timedelta(microseconds=1)
                    ),
                }
            )
            await uow.people.put(completed, expected_revision=1)
            return completed

    async def _scope_hash(
        self, store: PeopleStore, principal: Principal, rows: list[PeopleRecord]
    ) -> str:
        revisions = {str(row.id): row.revision for row in rows}
        for key in sorted({key for row in rows for key in row.support_ids}):
            source = await store.get(principal, key, ceiling=Sensitivity.RESTRICTED)
            if source is None:
                raise ConflictError("identity source changed")
            revisions[str(key)] = source.revision
        return hashlib.sha256(json.dumps(revisions, sort_keys=True).encode()).hexdigest()

    async def _person(
        self, store: PeopleStore, principal: Principal, record_id: UUID, ceiling: Sensitivity
    ) -> Person:
        row = await store.get(principal, record_id, ceiling=ceiling)
        if not isinstance(row, Person):
            raise NotFoundError("identity not found")
        return row

    def _operation(
        self,
        principal: Principal,
        operation: str,
        people: list[Person],
        assignments: list[IdentityAssignment],
    ) -> PeopleOperation:
        payload = {
            "operation": operation,
            "person_ids": [str(p.id) for p in people],
            "expected_revisions": {str(p.id): p.revision for p in people},
            "assignments": [a.model_dump(mode="json") for a in assignments],
        }
        return PeopleOperation.model_validate(
            {
                **payload,
                "id": self._ids.new_id(),
                "tenant_id": principal.tenant_id,
                "principal_id": principal.principal_id,
                "created_at": self._clock.now(),
                "updated_at": self._clock.now(),
                "expires_at": self._clock.now() + timedelta(minutes=10),
                "state": "preview",
                "request_hash": hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode()
                ).hexdigest(),
                "sensitivity": max(
                    (p.sensitivity for p in people), key=SENSITIVITY_ORDER.__getitem__
                ),
                "original_states": {str(p.id): p.state for p in people if p.state != "merged"},
            }
        )
