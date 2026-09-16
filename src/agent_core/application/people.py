"""Principal-first People management and source-backed profile projection."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.application.authorization import require_scope
from agent_core.application.people_belief_erasure import belief_erasure_id
from agent_core.application.people_erasure import PeopleErasureService
from agent_core.application.people_identity import ASSIGNABLE_KINDS, PeopleIdentityService
from agent_core.application.people_imports import PeopleImportService
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailThread
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.events import NewEvent
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import LIVE_MEMORY_STATUSES, SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.people import (
    OrganizationReference,
    PeopleCommitment,
    PeopleErasure,
    PeopleInteraction,
    PeopleOperation,
    PeopleQuery,
    PeopleRecord,
    PeopleRelationshipFilter,
    PeopleSource,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    PersonMention,
    RelationshipAssertion,
    normalize_identifier,
    referenced_organizations,
    referenced_people,
)
from agent_core.domain.people_imports import (
    PeopleImportCancel,
    PeopleImportRequest,
    PeopleImportView,
)
from agent_core.domain.people_views import (
    AddPersonAlias,
    CreatePerson,
    EndPersonAlias,
    IdentityEvidenceView,
    LegacyPeopleLinkResult,
    PeopleCorrectionRequest,
    PeopleCorrectionResult,
    PeopleErasureView,
    PeopleEvidenceView,
    PeopleForgetRequest,
    PeopleIdentityRequest,
    PeoplePage,
    PeopleSectionPage,
    PeopleSectionQuery,
    PersonProfile,
    UpdatePerson,
)
from agent_core.domain.views import MemoryView, Page
from agent_core.ports.determinism import Clock
from agent_core.ports.people_runtime import PeopleOwnerCorrections
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _key(principal: Principal, key: str) -> str:
    if not key or len(key) > 200:
        raise ToolValidationError("People idempotency key is invalid")
    return "people-write:" + _digest([principal.tenant_id, principal.principal_id, key])


def _safe(text: str) -> bool:
    return not contains_injection_pattern(text) and not contains_secret_material(text)


class PublicPeopleService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        *,
        surface_ceiling: Sensitivity = Sensitivity.SENSITIVE,
        identity: PeopleIdentityService | None = None,
        memory_for: Callable[[Principal], PeopleOwnerCorrections] | None = None,
        erasure: PeopleErasureService | None = None,
        legacy_linker: Callable[[Principal, int, str | None], Awaitable[LegacyPeopleLinkResult]]
        | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._surface_ceiling = surface_ceiling
        self._identity = identity
        self._memory_for = memory_for
        self._erasure = erasure
        self._legacy_linker = legacy_linker
        self.imports = PeopleImportService(uow_factory, clock)

    async def link_existing(
        self, principal: Principal, *, limit: int = 100, cursor: str | None = None
    ) -> LegacyPeopleLinkResult:
        require_scope(principal, "people.write")
        if self._legacy_linker is None:
            raise ConflictError("legacy linking is unavailable")
        return await self._legacy_linker(principal, limit, cursor)

    async def create_import(
        self, principal: Principal, request: PeopleImportRequest, *, key: str, ceiling: Sensitivity
    ) -> PeopleImportView:
        return await self.imports.submit(
            principal, request, key=key, ceiling=self._ceiling(ceiling)
        )

    async def list_imports(
        self,
        principal: Principal,
        *,
        ceiling: Sensitivity,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[PeopleImportView]:
        return await self.imports.list(
            principal, ceiling=self._ceiling(ceiling), limit=limit, cursor=cursor
        )

    async def get_import(
        self, principal: Principal, job_id: UUID, *, ceiling: Sensitivity
    ) -> PeopleImportView:
        return await self.imports.get(principal, job_id, ceiling=self._ceiling(ceiling))

    async def cancel_import(
        self,
        principal: Principal,
        job_id: UUID,
        request: PeopleImportCancel,
        *,
        ceiling: Sensitivity,
        key: str,
    ) -> PeopleImportView:
        return await self.imports.cancel(
            principal, job_id, request, key=key, ceiling=self._ceiling(ceiling)
        )

    def _ceiling(self, requested: Sensitivity) -> Sensitivity:
        return min((requested, self._surface_ceiling), key=SENSITIVITY_ORDER.__getitem__)

    async def _replay(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        key: str,
        request_hash: str,
        ceiling: Sensitivity,
    ) -> Person | None:
        event = await uow.events.get_by_derivation(key, principal)
        if event is None:
            return None
        if event.payload.get("request_hash") != request_hash:
            raise ConflictError("People idempotency key was reused")
        row = await uow.people.get(
            principal,
            UUID(event.payload["person_id"]),
            ceiling=ceiling,
            at_revision=int(event.payload["person_revision"]),
        )
        if not isinstance(row, Person):
            raise NotFoundError("person not found")
        return row

    async def _assertion(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        *,
        session_id: UUID,
        key: str,
        request_hash: str,
        person: Person,
        text: str,
    ) -> PeopleSource:
        event = await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type="people.owner_assertion",
                actor_type="user",
                actor_id=principal.principal_id,
                derivation_key=key,
                payload={
                    "content": text,
                    "request_hash": request_hash,
                    "person_id": str(person.id),
                    "person_revision": person.revision,
                },
            )
        )
        source = PeopleSource(
            id=uuid5(NAMESPACE_URL, key + ":source"),
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            created_at=self._clock.now(),
            updated_at=self._clock.now(),
            sensitivity=person.sensitivity,
            session_id=session_id,
            event_sequence=event.sequence,
            evidence_at=event.created_at,
            source_kind="owner",
            source_revision=request_hash,
        )
        await uow.people.put(source, expected_revision=0)
        return source

    async def create(
        self, principal: Principal, request: CreatePerson, *, key: str, ceiling: Sensitivity
    ) -> Person:
        require_scope(principal, "people.write")
        request = CreatePerson.model_validate(request.model_dump())
        ceiling = self._ceiling(ceiling)
        if SENSITIVITY_ORDER[request.sensitivity] > SENSITIVITY_ORDER[ceiling]:
            raise NotFoundError("person not found")
        name = request.display_name.strip()
        if not name or not _safe(name):
            raise ToolValidationError("People label contains refused content")
        operation_key = _key(principal, key)
        request_hash = _digest({"operation": "create", **request.model_dump(mode="json")})
        async with self._uow_factory() as uow:
            await uow.sessions.get(request.session_id, principal)
            async with uow.people.lock(principal):
                replay = await self._replay(uow, principal, operation_key, request_hash, ceiling)
                if replay is not None:
                    return replay
                person = Person(
                    id=uuid5(NAMESPACE_URL, operation_key + ":person"),
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    display_name=name,
                    state="active",
                    sensitivity=request.sensitivity,
                    created_at=self._clock.now(),
                    updated_at=self._clock.now(),
                )
                source = await self._assertion(
                    uow,
                    principal,
                    session_id=request.session_id,
                    key=operation_key,
                    request_hash=request_hash,
                    person=person,
                    text=f"Owner created person: {name}",
                )
                person = person.model_copy(update={"support_ids": [source.id]})
                await uow.people.put(person, expected_revision=0)
                return person

    async def update(
        self,
        principal: Principal,
        person_id: UUID,
        request: UpdatePerson,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> Person:
        require_scope(principal, "people.write")
        request = UpdatePerson.model_validate(request.model_dump())
        ceiling = self._ceiling(ceiling)
        operation_key = _key(principal, key)
        request_hash = _digest(
            {"operation": "update", "person_id": str(person_id), **request.model_dump(mode="json")}
        )
        if request.display_name is not None and (
            not request.display_name.strip() or not _safe(request.display_name)
        ):
            raise ToolValidationError("People label contains refused content")
        async with self._uow_factory() as uow:
            await uow.sessions.get(request.session_id, principal)
            async with uow.people.lock(principal):
                replay = await self._replay(uow, principal, operation_key, request_hash, ceiling)
                if replay is not None:
                    return replay
                current = await uow.people.get(principal, person_id, ceiling=ceiling)
                if not isinstance(current, Person):
                    raise NotFoundError("person not found")
                if current.revision != request.expected_revision or current.state == "merged":
                    raise ConflictError("person revision changed")
                if (
                    request.display_name is None
                    and request.pinned is None
                    and request.alias is None
                    and request.confirm is None
                ):
                    raise ToolValidationError(
                        "person update requires a label, pin, alias or confirmation"
                    )
                alias = None
                if request.alias is not None:
                    if not _safe(request.alias.model_dump_json()):
                        raise ToolValidationError("People alias contains refused content")
                    if isinstance(request.alias, AddPersonAlias):
                        try:
                            normalize_identifier(
                                request.alias.identifier_kind,
                                request.alias.namespace,
                                request.alias.value,
                            )
                        except ValueError as exc:
                            raise ToolValidationError("People alias is invalid") from exc
                        alias_id = uuid5(NAMESPACE_URL, operation_key + ":alias")
                        if await uow.people.is_erased(principal, alias_id):
                            raise ConflictError("People alias was erased")
                        alias = PersonIdentifier(
                            id=alias_id,
                            person_id=person_id,
                            tenant_id=principal.tenant_id,
                            principal_id=principal.principal_id,
                            created_at=self._clock.now(),
                            updated_at=self._clock.now(),
                            sensitivity=current.sensitivity,
                            identifier_kind=request.alias.identifier_kind,
                            namespace=request.alias.namespace,
                            value=request.alias.value.strip(),
                            context=request.alias.context,
                            verification="owner_confirmed",
                            valid_from=request.alias.valid_from or self._clock.now(),
                        )
                    elif isinstance(request.alias, EndPersonAlias):
                        existing = await uow.people.get(
                            principal, request.alias.identifier_id, ceiling=ceiling
                        )
                        if (
                            not isinstance(existing, PersonIdentifier)
                            or existing.person_id != person_id
                        ):
                            raise NotFoundError("person alias not found")
                        if (
                            existing.revision != request.alias.expected_revision
                            or existing.valid_to is not None
                        ):
                            raise ConflictError("person alias revision changed")
                        if request.alias.valid_to <= existing.valid_from:
                            raise ToolValidationError("alias end must follow its start")
                        alias = existing.model_copy(
                            update={
                                "valid_to": request.alias.valid_to,
                                "revision": existing.revision + 1,
                                "updated_at": max(
                                    self._clock.now(),
                                    existing.updated_at + timedelta(microseconds=1),
                                ),
                            }
                        )
                updated = current.model_copy(
                    update={
                        "state": "active" if request.confirm else current.state,
                        "display_name": request.display_name.strip()
                        if request.display_name is not None
                        else current.display_name,
                        "pinned": current.pinned if request.pinned is None else request.pinned,
                        "revision": current.revision + 1,
                        "updated_at": max(
                            self._clock.now(), current.updated_at + timedelta(microseconds=1)
                        ),
                    }
                )
                if request.confirm and alias is None:
                    alias = PersonIdentifier(
                        id=uuid5(NAMESPACE_URL, operation_key + ":confirmed-name"),
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        person_id=person_id,
                        created_at=self._clock.now(),
                        updated_at=self._clock.now(),
                        sensitivity=current.sensitivity,
                        identifier_kind="name",
                        namespace="owner",
                        value=updated.display_name,
                        context="owner",
                        verification="owner_confirmed",
                        valid_from=self._clock.now(),
                    )
                source = await self._assertion(
                    uow,
                    principal,
                    session_id=request.session_id,
                    key=operation_key,
                    request_hash=request_hash,
                    person=updated,
                    text=(
                        f"Owner confirmed identity: {updated.display_name}"
                        if request.confirm
                        else f"Owner updated person: {updated.display_name}"
                    )
                    + (f"; alias {request.alias.model_dump_json()}" if request.alias else ""),
                )
                updated = updated.model_copy(update={"support_ids": [source.id]})
                await uow.people.put(updated, expected_revision=current.revision)
                if alias is not None:
                    await uow.people.put(
                        alias.model_copy(update={"support_ids": [source.id]}),
                        expected_revision=alias.revision - 1,
                    )
                return updated

    async def get(
        self, principal: Principal, person_id: UUID, *, ceiling: Sensitivity
    ) -> PersonProfile:
        require_scope(principal, "people.read")
        ceiling = self._ceiling(ceiling)
        async with self._uow_factory() as uow, uow.people.lock(principal):
            person = await uow.people.get(principal, person_id, ceiling=ceiling)
            if not isinstance(person, Person) or not _safe(person.display_name):
                raise NotFoundError("person not found")
            profile = PersonProfile(person=person)
            query = PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                person_id=person.id,
                sensitivity_ceiling=ceiling,
                limit=100,
                kinds=["identifier", "memory_link", "relationship", "interaction", "commitment"],
            )
            seen_beliefs: set[UUID] = set()
            for _ in range(10):
                rows = await uow.people.query(query)
                for row in rows[:100]:
                    if not _safe(row.model_dump_json()):
                        continue
                    belief = None
                    if isinstance(row, (PersonMemoryLink, RelationshipAssertion, PeopleCommitment)):
                        if row.unresolved:
                            continue
                        try:
                            belief = await uow.memories.get(row.belief_id, principal)
                        except NotFoundError:
                            continue
                        if (
                            SENSITIVITY_ORDER[belief.sensitivity] > SENSITIVITY_ORDER[ceiling]
                            or belief.status not in LIVE_MEMORY_STATUSES
                            or belief.valid_from > self._clock.now()
                            or (
                                belief.valid_to is not None and belief.valid_to <= self._clock.now()
                            )
                            or (
                                belief.expires_at is not None
                                and belief.expires_at <= self._clock.now()
                            )
                            or not _safe(belief.statement)
                        ):
                            continue
                    if isinstance(row, PersonMemoryLink) and belief is not None:
                        if belief.id not in seen_beliefs:
                            profile.facts.append(MemoryView.from_record(belief))
                            profile.fact_revisions[belief.id] = belief.store_position
                            seen_beliefs.add(belief.id)
                    elif isinstance(row, PersonIdentifier):
                        profile.aliases.append(row)
                    elif isinstance(row, RelationshipAssertion):
                        profile.relationships.append(row)
                    elif isinstance(row, PeopleInteraction):
                        profile.history.append(row)
                    elif isinstance(row, PeopleCommitment):
                        profile.commitments.append(row)
                if len(rows) <= 100:
                    break
                query = query.model_copy(update={"after": rows[99].id})
            truncated = len(rows) > 100 or (
                max(
                    len(profile.aliases),
                    len(profile.relationships),
                    len(profile.history),
                    len(profile.commitments),
                    len(profile.facts),
                )
                > 20
            )
            profile.history.sort(
                key=lambda row: (
                    row.occurred_at is not None,
                    row.occurred_at.timestamp() if row.occurred_at else 0,
                    -row.id.int,
                ),
                reverse=True,
            )
            related_ids = (
                set().union(
                    *(
                        referenced_people(row) | referenced_organizations(row)
                        for row in profile.relationships[:20]
                    )
                )
                if profile.relationships
                else set()
            )
            for related_id in sorted(related_ids)[:40]:
                related = await uow.people.get(principal, related_id, ceiling=ceiling)
                if isinstance(related, (Person, OrganizationReference)) and _safe(
                    related.display_name
                ):
                    profile.related_labels[related.id] = related.display_name
            return profile.model_copy(
                update={
                    "aliases": profile.aliases[:20],
                    "relationships": profile.relationships[:20],
                    "history": profile.history[:20],
                    "commitments": profile.commitments[:20],
                    "facts": profile.facts[:20],
                    "fact_revisions": {
                        fact.id: profile.fact_revisions[fact.id] for fact in profile.facts[:20]
                    },
                    "truncated": truncated,
                }
            )

    async def list(
        self,
        principal: Principal,
        *,
        ceiling: Sensitivity,
        text: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        as_of: datetime | None = None,
        state: str | None = None,
        pinned: bool | None = None,
        sort: str = "id",
        relationship: PeopleRelationshipFilter | None = None,
    ) -> PeoplePage:
        require_scope(principal, "people.read")
        ceiling = self._ceiling(ceiling)
        normalized = None if not text else normalize_identifier("name", "owner", text)
        if state not in {None, "active", "provisional", "merged"}:
            raise ToolValidationError("invalid People directory state")
        if sort not in {"id", "recent"}:
            raise ToolValidationError("invalid People directory sort")
        query = PeopleQuery(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            sort="recent" if sort == "recent" else "id",
            relationship=relationship,
            text=normalized,
            search_aliases=True,
            states=[state] if state else None,
            pinned=pinned,
            as_of=as_of or self._clock.now(),
            sensitivity_ceiling=ceiling,
            limit=min(max(limit, 1), 100),
        )
        binding = _digest(
            {
                "query": query.model_dump(mode="json", exclude={"as_of"}),
                "requested_as_of": as_of.isoformat() if as_of else None,
            }
        )
        after = None
        watermark = None
        if cursor is not None:
            try:
                if len(cursor) > 2048:
                    raise ValueError("cursor too long")
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if decoded["binding"] != binding:
                    raise ValueError("cursor binding changed")
                after = UUID(decoded["after"])
                instant = datetime.fromisoformat(decoded["as_of"])
                if instant.tzinfo is None:
                    raise ValueError("cursor time must be aware")
                query = query.model_copy(update={"as_of": instant})
                watermark = decoded["watermark"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ConflictError("People cursor is invalid; restart the list") from exc
        query = query.model_copy(update={"after": after})
        async with self._uow_factory() as uow:
            before = [
                await uow.people.watermark(principal),
                await uow.memories.head_position(principal),
            ]
            if watermark is not None and watermark != before:
                raise ConflictError("People changed; restart the list")
            rows = await uow.people.query(query)
            if [
                await uow.people.watermark(principal),
                await uow.memories.head_position(principal),
            ] != before:
                raise ConflictError("People changed; restart the list")
        items = [r for r in rows[: query.limit] if isinstance(r, Person) and _safe(r.display_name)]
        next_cursor = None
        if len(rows) > query.limit:
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "binding": binding,
                        "as_of": query.as_of.isoformat() if query.as_of else None,
                        "watermark": before,
                        "after": str(rows[query.limit - 1].id),
                    },
                    sort_keys=True,
                ).encode()
            ).decode()
        return PeoplePage(items=items, next_cursor=next_cursor)

    async def section(
        self,
        principal: Principal,
        person_id: UUID,
        request: PeopleSectionQuery,
        *,
        ceiling: Sensitivity,
        run_id: UUID | None = None,
    ) -> PeopleSectionPage:
        require_scope(principal, "people.read")
        request = PeopleSectionQuery.model_validate(request.model_dump())
        if (
            request.since is not None
            and request.until is not None
            and request.since >= request.until
        ):
            raise ToolValidationError("history interval must be positive")
        ceiling = self._ceiling(ceiling)
        query = PeopleQuery(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            person_id=person_id,
            sensitivity_ceiling=ceiling,
            limit=100,
            kinds=ASSIGNABLE_KINDS
            if request.section == "identity-evidence"
            else [
                {"history": "interaction", "relationships": "relationship", "facts": "memory_link"}[
                    request.section
                ]
            ],
            as_of=request.as_of,
            known_at=request.known_at,
            since=request.since,
            until=request.until,
            channel=request.channel,
            interaction_kind=request.interaction_kind,
            unknown_time=request.unknown_time,
            sort="history" if request.section == "history" else "id",
        )
        binding = _digest(
            [query.model_dump(mode="json"), request.model_dump(mode="json", exclude={"cursor"})]
        )
        watermark = None
        if request.cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(request.cursor))
                if decoded["binding"] != binding:
                    raise ValueError("cursor binding")
                watermark = decoded["watermark"]
                query = query.model_copy(
                    update={
                        "after": UUID(decoded["after"]),
                        "after_event_at": datetime.fromisoformat(decoded["event_at"])
                        if decoded["event_at"]
                        else None,
                    }
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise ConflictError("People cursor is invalid; restart the list") from exc
        items: list[
            RelationshipAssertion | PeopleInteraction | MemoryView | IdentityEvidenceView
        ] = []
        fact_revisions: dict[UUID, int] = {}
        seen_beliefs: set[UUID] = set()
        last: PeopleRecord | None = None
        has_more = False
        async with self._uow_factory() as uow, uow.people.lock(principal):
            person = await uow.people.get(
                principal, person_id, ceiling=ceiling, known_at=request.known_at
            )
            if not isinstance(person, Person) or not _safe(person.display_name):
                raise NotFoundError("person not found")
            before = [
                await uow.people.watermark(principal),
                await uow.memories.head_position(principal),
            ]
            if watermark is not None and watermark != before:
                raise ConflictError("People changed; restart the list")
            for _ in range(10):
                rows = await uow.people.query(query)
                for row in rows[:100]:
                    if not _safe(row.model_dump_json()):
                        continue
                    view: (
                        RelationshipAssertion
                        | PeopleInteraction
                        | MemoryView
                        | IdentityEvidenceView
                    )
                    if request.section == "identity-evidence":
                        repair_view = await self._identity_evidence(
                            uow, principal, row, ceiling, known_at=request.known_at
                        )
                        if repair_view is None:
                            continue
                        view = repair_view
                    elif isinstance(row, PeopleInteraction):
                        view = row
                    elif isinstance(row, (PersonMemoryLink, RelationshipAssertion)):
                        if row.unresolved:
                            continue
                        try:
                            belief = (
                                await uow.memories.get_at(
                                    row.belief_id, principal, known_at=request.known_at
                                )
                                if request.known_at is not None
                                else await uow.memories.get(row.belief_id, principal)
                            )
                        except NotFoundError:
                            continue
                        instant = request.as_of or self._clock.now()
                        if (
                            SENSITIVITY_ORDER[belief.sensitivity] > SENSITIVITY_ORDER[ceiling]
                            or not _safe(belief.statement)
                            or (
                                request.known_at is not None
                                and belief.created_at > request.known_at
                            )
                        ):
                            continue
                        if not request.include_inactive and (
                            (request.as_of is None and belief.status not in LIVE_MEMORY_STATUSES)
                            or belief.valid_from > instant
                            or (belief.valid_to is not None and belief.valid_to <= instant)
                            or (belief.expires_at is not None and belief.expires_at <= instant)
                        ):
                            continue
                        if isinstance(row, PersonMemoryLink):
                            if belief.id in seen_beliefs:
                                continue
                            seen_beliefs.add(belief.id)
                            view = MemoryView.from_record(belief)
                        else:
                            view = row
                    else:
                        continue
                    if len(items) == request.limit:
                        has_more = True
                        break
                    items.append(view)
                    if isinstance(view, MemoryView):
                        fact_revisions[view.id] = belief.store_position
                    last = row
                if has_more or len(rows) <= 100:
                    break
                last_scanned = rows[99]
                query = query.model_copy(
                    update={
                        "after": last_scanned.id,
                        "after_event_at": last_scanned.occurred_at
                        if isinstance(last_scanned, PeopleInteraction)
                        else None,
                    }
                )
            else:
                # Bound sparse filtered walks without claiming complete coverage.
                has_more = True
                last = last_scanned
            after = [
                await uow.people.watermark(principal),
                await uow.memories.head_position(principal),
            ]
            if before != after:
                raise ConflictError("People changed; restart the list")
            if run_id is not None:
                run = await uow.runs.get(run_id, principal)
                await uow.events.append(
                    NewEvent(
                        session_id=run.session_id,
                        run_id=run.id,
                        event_type="people.context.used",
                        actor_type="memory",
                        payload={"record_ids": [str(person_id), *(str(item.id) for item in items)]},
                    )
                )
        cursor = None
        if has_more and last is not None:
            cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "binding": binding,
                        "watermark": before,
                        "after": str(last.id),
                        "event_at": last.occurred_at.isoformat()
                        if isinstance(last, PeopleInteraction) and last.occurred_at
                        else None,
                    },
                    sort_keys=True,
                ).encode()
            ).decode()
        return PeopleSectionPage(items=items, next_cursor=cursor, fact_revisions=fact_revisions)

    async def _identity_evidence(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        row: PeopleRecord,
        ceiling: Sensitivity,
        *,
        known_at: datetime | None = None,
    ) -> IdentityEvidenceView | None:
        belief_id = None
        unresolved = False
        if isinstance(row, (PersonMemoryLink, RelationshipAssertion, PeopleCommitment)):
            try:
                belief = (
                    await uow.memories.get_at(row.belief_id, principal, known_at=known_at)
                    if known_at is not None
                    else await uow.memories.get(row.belief_id, principal)
                )
            except NotFoundError:
                return None
            if SENSITIVITY_ORDER[belief.sensitivity] > SENSITIVITY_ORDER[ceiling] or not _safe(
                belief.statement
            ):
                return None
            label = belief.statement
            belief_id = belief.id
            unresolved = row.unresolved
        elif isinstance(row, PersonIdentifier):
            label = row.value
        elif isinstance(row, PersonMention):
            label = f"{row.role.capitalize()} mention · characters {row.start}-{row.end}"
        elif isinstance(row, PeopleInteraction):
            label = row.summary
        else:
            return None
        return IdentityEvidenceView(
            id=row.id,
            revision=row.revision,
            kind=row.kind,
            label=label,
            belief_id=belief_id,
            support_ids=row.support_ids,
            unresolved=unresolved,
        )

    async def evidence(
        self, principal: Principal, person_id: UUID, reference: UUID, *, ceiling: Sensitivity
    ) -> PeopleEvidenceView:
        require_scope(principal, "people.read")
        ceiling = self._ceiling(ceiling)
        async with self._uow_factory() as uow:
            person = await uow.people.get(principal, person_id, ceiling=ceiling)
            source = await uow.people.get(principal, reference, ceiling=ceiling)
            if not isinstance(person, Person) or not isinstance(source, PeopleSource):
                raise NotFoundError("People evidence not found")
            linked = reference in person.support_ids
            if not linked:
                linked = bool(
                    await uow.people.query(
                        PeopleQuery(
                            tenant_id=principal.tenant_id,
                            principal_id=principal.principal_id,
                            person_id=person_id,
                            source_id=reference,
                            sensitivity_ceiling=ceiling,
                            kinds=[
                                "identifier",
                                "mention",
                                "memory_link",
                                "relationship",
                                "interaction",
                                "commitment",
                            ],
                            limit=1,
                        )
                    )
                )
            if not linked:
                raise NotFoundError("People evidence not found")
            require_scope(
                principal, "email.read" if source.source_kind == "email" else "session.read"
            )
            await uow.sessions.get(source.session_id, principal)
            owner_assertion = None
            if source.source_kind == "owner":
                events = await uow.events.list_after(
                    source.session_id, source.event_sequence - 1, principal, limit=1
                )
                event = next((row for row in events if row.sequence == source.event_sequence), None)
                if (
                    event is not None
                    and event.event_type == "people.owner_assertion"
                    and event.actor_id == principal.principal_id
                ):
                    content = event.payload.get("content")
                    if isinstance(content, str) and len(content) <= 16384:
                        owner_assertion = content
            email_thread_id = None
            if source.source_kind == "email":
                source_key = hashlib.sha256(
                    f"{source.account_id}:{source.thread_id}".encode()
                ).hexdigest()
                index = await uow.email.get(principal, "thread_source", source_key)
                if index is not None:
                    row = await uow.email.get(
                        principal, "thread", str(index.payload.get("thread_id", ""))
                    )
                    if row is not None:
                        thread = EmailThread.model_validate(row.payload)
                        if (
                            thread.account_id == source.account_id
                            and thread.provider_thread_id == source.thread_id
                            and any(message.id == source.message_id for message in thread.messages)
                        ):
                            email_thread_id = thread.id
            return PeopleEvidenceView(
                reference=reference,
                source_kind=source.source_kind,
                session_id=source.session_id,
                event_sequence=source.event_sequence,
                evidence_at=source.evidence_at,
                account_id=source.account_id,
                thread_id=source.thread_id,
                message_id=source.message_id,
                email_thread_id=email_thread_id,
                owner_assertion=owner_assertion,
            )

    async def identity_operation(
        self,
        principal: Principal,
        request: PeopleIdentityRequest,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleOperation:
        if self._identity is None:
            raise NotFoundError("identity operation not found")
        return await self._identity.request(
            principal, request, key=key, ceiling=self._ceiling(ceiling)
        )

    async def operation(
        self, principal: Principal, operation_id: UUID, *, ceiling: Sensitivity
    ) -> PeopleOperation | PeopleErasureView:
        require_scope(principal, "people.read")
        async with self._uow_factory() as uow:
            row = await uow.people.get(principal, operation_id, ceiling=self._ceiling(ceiling))
            if isinstance(row, PeopleErasure) and self._erasure is not None:
                return await self._erasure.get(
                    principal, operation_id, ceiling=self._ceiling(ceiling)
                )
            if not isinstance(row, PeopleOperation):
                raise NotFoundError("identity operation not found")
            return row

    async def correct(
        self,
        principal: Principal,
        person_id: UUID,
        request: PeopleCorrectionRequest,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleCorrectionResult:
        require_scope(principal, "people.write")
        if self._memory_for is None:
            raise NotFoundError("People correction is unavailable")
        request = PeopleCorrectionRequest.model_validate(request.model_dump())
        if request.operation in {"correct", "changed"}:
            if (
                not request.statement
                or not request.statement.strip()
                or not _safe(request.statement)
            ):
                raise ToolValidationError("correction requires a safe statement")
        elif request.statement is not None:
            raise ToolValidationError("unexpected correction statement")
        if request.projection is not None and request.operation not in {"correct", "changed"}:
            raise ToolValidationError("structured replacement requires a correction or change")
        ceiling = self._ceiling(ceiling)
        operation_key = _key(principal, key) + ":correction"
        digest = _digest([str(person_id), request.model_dump(mode="json")])
        async with (
            self._uow_factory() as uow,
            uow.email.lock(principal),
            uow.people.lock(principal),
        ):
            await uow.sessions.get(request.session_id, principal)
            person = await uow.people.get(principal, person_id, ceiling=ceiling)
            if not isinstance(person, Person) or person.state == "merged":
                raise NotFoundError("person not found")
            receipt = await uow.events.get_by_derivation(operation_key + ":completed", principal)
            if receipt is not None:
                if receipt.payload["request_hash"] != digest:
                    raise ConflictError("People idempotency key was reused")
                replay = PeopleCorrectionResult.model_validate(receipt.payload["result"])
                if replay.belief is not None:
                    live = await uow.memories.get(replay.belief.id, principal)
                    if SENSITIVITY_ORDER[live.sensitivity] > SENSITIVITY_ORDER[ceiling]:
                        raise NotFoundError("person not found")
                return replay
            if person.revision != request.expected_revision:
                raise ConflictError("person revision changed")
            belief = await uow.memories.get(request.belief_id, principal)
            if SENSITIVITY_ORDER[belief.sensitivity] > SENSITIVITY_ORDER[ceiling]:
                raise NotFoundError("memory not found")
            if belief.store_position != request.expected_position:
                raise ConflictError("memory revision changed")
            if request.effective_at is not None and (
                request.operation != "changed"
                or not belief.valid_from <= request.effective_at <= self._clock.now()
            ):
                raise ToolValidationError("change date must fall between the fact start and now")
            from agent_core.application.people_identity import assignments_for

            links = await assignments_for(uow.people, principal, person_id, ceiling)
            affected = [
                r
                for r in links
                if isinstance(r, (PersonMemoryLink, RelationshipAssertion, PeopleCommitment))
                and r.belief_id == belief.id
            ]
            if not affected:
                raise NotFoundError("person fact not found")
            projection: RelationshipAssertion | PeopleCommitment | None = None
            if request.projection is not None:
                selected = next((row for row in affected if row.id == request.projection.id), None)
                if not isinstance(selected, (RelationshipAssertion, PeopleCommitment)) or (
                    selected.kind != request.projection.kind
                ):
                    raise NotFoundError("person projection not found")
                if selected.revision != request.projection.expected_revision:
                    raise ConflictError("person projection revision changed")
                values = selected.model_dump() | request.projection.model_dump(
                    exclude={"id", "expected_revision", "kind"}
                )
                projection = type(selected).model_validate(values)
                if person_id not in referenced_people(projection) or not _safe(
                    projection.model_dump_json()
                ):
                    raise ToolValidationError("replacement must describe this person safely")
                for endpoint_id in referenced_people(projection):
                    endpoint = await uow.people.get(principal, endpoint_id, ceiling=ceiling)
                    if not isinstance(endpoint, Person) or endpoint.state == "merged":
                        raise NotFoundError("relationship endpoint not found")
                for endpoint_id in referenced_organizations(projection):
                    endpoint = await uow.people.get(principal, endpoint_id, ceiling=ceiling)
                    if not isinstance(endpoint, OrganizationReference):
                        raise NotFoundError("relationship endpoint not found")
            source_id = uuid5(NAMESPACE_URL, operation_key + ":source")
            updated = Person.model_validate(
                person.model_dump()
                | {
                    "revision": person.revision + 1,
                    "updated_at": max(
                        self._clock.now(), person.updated_at + timedelta(microseconds=1)
                    ),
                    "support_ids": [source_id],
                }
            )
            source = await self._assertion(
                uow,
                principal,
                session_id=request.session_id,
                key=operation_key,
                request_hash=digest,
                person=updated,
                text=(request.statement or f"Owner {request.operation} fact {belief.id}")
                + (
                    "\nOwner structured correction: " + request.projection.model_dump_json()
                    if request.projection is not None
                    else ""
                ),
            )
            result = await self._memory_for(principal).correct_from_owner(
                belief.id,
                session_id=request.session_id,
                source_event_id=source.event_sequence,
                operation=request.operation,
                statement=request.statement,
                effective_at=request.effective_at,
                expected_position=request.expected_position,
                existing_uow=uow,
            )
            for row in affected:
                if request.operation == "remove":
                    await uow.people.erase(principal, [row.id])
                elif isinstance(row, (RelationshipAssertion, PeopleCommitment)):
                    retained: dict[str, object] = {}
                    if request.operation == "affirm":
                        assert result is not None
                        retained = {
                            "belief_id": result.id,
                            "support_ids": [source.id],
                        }
                        if isinstance(row, PeopleCommitment):
                            retained["state_source_id"] = source.id
                    elif request.operation == "changed" and isinstance(row, RelationshipAssertion):
                        retained["valid_to"] = request.effective_at or self._clock.now()
                    await uow.people.put(
                        row.model_copy(
                            update={
                                **retained,
                                "unresolved": row.unresolved
                                if request.operation in {"affirm", "changed"}
                                else True,
                                "revision": row.revision + 1,
                                "updated_at": max(
                                    self._clock.now(), row.updated_at + timedelta(microseconds=1)
                                ),
                            }
                        ),
                        expected_revision=row.revision,
                    )
            if projection is not None:
                assert result is not None
                fields = projection.model_dump() | {
                    "id": uuid5(NAMESPACE_URL, operation_key + ":projection"),
                    "revision": 1,
                    "created_at": self._clock.now(),
                    "updated_at": self._clock.now(),
                    "belief_id": result.id,
                    "support_ids": [source.id],
                    "unresolved": False,
                    "sensitivity": max(
                        (projection.sensitivity, result.sensitivity),
                        key=SENSITIVITY_ORDER.__getitem__,
                    ),
                }
                if isinstance(projection, RelationshipAssertion):
                    fields.update(valid_from=result.valid_from, valid_to=None)
                    replacement_projection: RelationshipAssertion | PeopleCommitment = (
                        RelationshipAssertion.model_validate(fields)
                    )
                else:
                    fields.update(state_source_id=source.id, interaction_ids=[])
                    replacement_projection = PeopleCommitment.model_validate(fields)
                await uow.people.put(replacement_projection, expected_revision=0)
            if result is not None and request.operation != "reject":
                await uow.people.put(
                    PersonMemoryLink(
                        id=uuid5(NAMESPACE_URL, operation_key + ":link"),
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        person_id=person_id,
                        belief_id=result.id,
                        sensitivity=result.sensitivity,
                        created_at=self._clock.now(),
                        updated_at=self._clock.now(),
                        support_ids=[source.id],
                    ),
                    expected_revision=0,
                )
            await uow.people.put(updated, expected_revision=person.revision)
            removal = None
            if request.operation == "remove":
                erasure_receipt = await uow.people.get(
                    principal, belief_erasure_id(belief.id), ceiling=ceiling
                )
                if isinstance(erasure_receipt, PeopleErasure):
                    removal = PeopleErasureService.view(erasure_receipt).model_copy(
                        update={
                            "scope": "One fact and its generated copies; original messages remain."
                        }
                    )
            response = PeopleCorrectionResult(
                person_revision=updated.revision,
                belief=None if result is None else MemoryView.from_record(result),
                removed=result is None,
                erasure=removal,
            )
            await uow.events.append(
                NewEvent(
                    session_id=request.session_id,
                    run_id=None,
                    event_type="people.correction_completed",
                    actor_type="user",
                    actor_id=principal.principal_id,
                    derivation_key=operation_key + ":completed",
                    payload={"request_hash": digest, "result": response.model_dump(mode="json")},
                )
            )
            return response

    async def forget(
        self,
        principal: Principal,
        person_id: UUID,
        request: PeopleForgetRequest,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleErasureView:
        if self._erasure is None:
            raise NotFoundError("People erasure is unavailable")
        return await self._erasure.forget(
            principal, person_id, request, key=key, ceiling=self._ceiling(ceiling)
        )
