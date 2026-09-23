"""Task-sized People recall using the governed belief ranker and shared budget."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.application.people import _safe
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError, ToolValidationError
from agent_core.domain.memory import (
    LIVE_MEMORY_STATUSES,
    SENSITIVITY_ORDER,
    MemoryCorrection,
    RecallQuery,
    RecallResult,
    Sensitivity,
    TracedPersonContext,
)
from agent_core.domain.people import (
    OrganizationReference,
    PeopleCommitment,
    PeopleInteraction,
    PeopleQuery,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    RelationshipAssertion,
    is_non_person_reference,
    referenced_people,
)
from agent_core.ports.people_runtime import PeopleRecall
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class PeopleContextService:
    def __init__(
        self,
        factory: UnitOfWorkFactory,
        retriever: PeopleRecall,
        *,
        surface_ceiling: Sensitivity = Sensitivity.SENSITIVE,
    ) -> None:
        self._factory = factory
        self._retriever = retriever
        self._surface_ceiling = surface_ceiling

    async def recall(
        self,
        principal: Principal,
        person_ids: list[UUID],
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
        include_ordinary: bool = False,
        measure_rendered_tokens: Callable[[str], int] | None = None,
    ) -> RecallResult:
        require_scope(principal, "people.read")
        if not 1 <= len(person_ids) <= 3 or len(set(person_ids)) != len(person_ids):
            raise ToolValidationError("People context requires one to three distinct identities")
        if (principal.tenant_id, principal.principal_id) != (query.tenant_id, query.principal_id):
            raise NotFoundError("person not found")
        ceiling = min(
            (query.sensitivity_ceiling, self._surface_ceiling), key=SENSITIVITY_ORDER.__getitem__
        )
        belief_ids: set[UUID] = set()
        context: list[TracedPersonContext] = []
        neighbors: set[UUID] = set()
        projected: set[UUID] = set()
        async with self._factory() as uow, uow.people.lock(principal):
            await uow.sessions.get(session_id, principal)
            for person_id in person_ids:
                person = await uow.people.get(
                    principal, person_id, ceiling=ceiling, known_at=query.known_at
                )
                if not isinstance(person, Person) or not _safe(person.display_name):
                    raise NotFoundError("person not found")
                context.append(
                    TracedPersonContext(
                        record_id=person.id,
                        revision=person.revision,
                        person_ids=[person.id],
                        kind="person",
                        text=f"Person: {person.display_name} ({person.state}).",
                        sensitivity=person.sensitivity,
                        source_ids=person.support_ids,
                    )
                )
                links = PeopleQuery(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    person_id=person_id,
                    sensitivity_ceiling=ceiling,
                    kinds=["memory_link", "relationship", "commitment"],
                    as_of=query.as_of,
                    known_at=query.known_at,
                    limit=100,
                )
                for _ in range(10):
                    page = await uow.people.query(links)
                    for row in page[:100]:
                        if (
                            isinstance(
                                row, (PersonMemoryLink, RelationshipAssertion, PeopleCommitment)
                            )
                            and not row.unresolved
                        ):
                            belief_ids.add(row.belief_id)
                            if (
                                isinstance(row, (RelationshipAssertion, PeopleCommitment))
                                and row.id not in projected
                            ):
                                item = await self._relationship_context(
                                    uow, principal, row, query, ceiling, set(person_ids), neighbors
                                )
                                if item is not None:
                                    context.append(item)
                                    projected.add(row.id)

                    if len(page) <= 100 or len(belief_ids) >= 1000:
                        break
                    links = links.model_copy(update={"after": page[99].id})
                history = await uow.people.query(
                    links.model_copy(
                        update={
                            "kinds": ["interaction"],
                            "after": None,
                            "sort": "history",
                            "limit": 5,
                            "until": None,
                            "unknown_time": "exclude" if query.as_of else "include",
                        }
                    )
                )
                for row in history[:5]:
                    if not isinstance(row, PeopleInteraction) or not _safe(row.summary):
                        continue
                    when = row.occurred_at.isoformat() if row.occurred_at else "date unknown"
                    roles = ", ".join(p.role for p in row.participants if p.person_id == person_id)
                    context.append(
                        TracedPersonContext(
                            record_id=row.id,
                            revision=row.revision,
                            person_ids=sorted(referenced_people(row)),
                            kind="interaction",
                            text=(
                                f"{person.display_name}: {row.attribution}; {roles}; "
                                f"{when}; precision {row.precision}; "
                                f"source timezone {row.source_timezone or 'unknown'}; {row.summary}"
                            ),
                            sensitivity=row.sensitivity,
                            source_ids=row.support_ids,
                        )
                    )
            effective = query.model_copy(
                update={
                    "include_ids": query.include_ids
                    if include_ordinary
                    else tuple(sorted(belief_ids)[:1000]),
                    "expand_ids": tuple(sorted(belief_ids)[:1000]) if include_ordinary else (),
                    "subjects": [],
                    "people_scope": tuple(person_ids),
                    "sensitivity_ceiling": ceiling,
                    "budget_tokens": min(query.budget_tokens, 2000),
                    "max_items": min(query.max_items, 20),
                }
            )
            return await self._retriever.recall(
                effective,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id or run_id,
                moment=moment,
                surface_id=surface_id,
                people_items=context,
                existing_uow=uow,
                measure_rendered_tokens=measure_rendered_tokens,
            )

    async def _relationship_context(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        row: RelationshipAssertion | PeopleCommitment,
        query: RecallQuery,
        ceiling: Sensitivity,
        focal: set[UUID],
        neighbors: set[UUID],
    ) -> TracedPersonContext | None:
        if isinstance(row, PeopleCommitment) and row.state not in {"open", "proposed", "uncertain"}:
            return None
        try:
            belief = (
                await uow.memories.get_at(row.belief_id, principal, known_at=query.known_at)
                if query.known_at is not None
                else await uow.memories.get(row.belief_id, principal)
            )
        except NotFoundError:
            return None
        at = query.as_of or self._retriever.current_time()
        if (
            SENSITIVITY_ORDER[belief.sensitivity] > SENSITIVITY_ORDER[ceiling]
            or (query.as_of is None and belief.status not in LIVE_MEMORY_STATUSES)
            or belief.valid_from > at
            or (belief.valid_to is not None and belief.valid_to <= at)
            or (belief.expires_at is not None and belief.expires_at <= at)
            or not _safe(belief.statement)
        ):
            return None
        endpoints = (
            (row.subject, row.object)
            if isinstance(row, RelationshipAssertion)
            else (row.debtor, row.beneficiary)
        )
        extra = {endpoint.id for endpoint in endpoints if endpoint.id is not None} - focal
        if len(neighbors | extra) > 10:
            return None
        labels = []
        for endpoint in endpoints:
            if endpoint.id is None:
                labels.append("owner")
            else:
                entity = await uow.people.get(
                    principal, endpoint.id, ceiling=ceiling, known_at=query.known_at
                )
                if not isinstance(entity, (Person, OrganizationReference)) or not _safe(
                    entity.display_name
                ):
                    return None
                labels.append(entity.display_name)
        if isinstance(row, RelationshipAssertion):
            start = row.valid_from.isoformat() if row.valid_from else "start unknown"
            detail = (
                f"{labels[0]} --{row.predicate}--> {labels[1]}; {row.qualifier}; "
                f"effective {start}; precision {row.precision}; "
                f"source timezone {row.source_timezone or 'unknown'}."
            )
        else:
            due = row.due_at.isoformat() if row.due_at else "unknown"
            detail = (
                f"Commitment: {labels[0]} owes {labels[1]}; state {row.state}; "
                f"due {due}; precision {row.due_precision}; "
                f"source timezone {row.source_timezone or 'unknown'}; {row.description}."
            )
        detail += (
            f" {belief.derivation.value}; last evidence {belief.last_evidence_at.isoformat()}."
        )
        if not _safe(detail):
            return None
        neighbors.update(extra)
        return TracedPersonContext(
            record_id=row.id,
            revision=row.revision,
            person_ids=sorted(referenced_people(row)),
            kind="relationship" if isinstance(row, RelationshipAssertion) else "commitment",
            text=detail,
            sensitivity=max(
                (row.sensitivity, belief.sensitivity), key=SENSITIVITY_ORDER.__getitem__
            ),
            source_ids=row.support_ids,
        )

    async def revision(self, principal: Principal) -> int:
        async with self._factory() as uow:
            people_revision = await uow.people.watermark(principal)
            memory_revision = await uow.memories.head_position(principal)
            total = people_revision + memory_revision
            return total * (total + 1) // 2 + memory_revision

    async def automatic_recall(
        self,
        principal: Principal,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
        measure_rendered_tokens: Callable[[str], int] | None = None,
    ) -> RecallResult:
        ids: list[UUID] = []
        person_query = False
        if "people.read" in principal.scopes and query.text and moment == "in_turn":
            ceiling = min(
                (query.sensitivity_ceiling, self._surface_ceiling),
                key=SENSITIVITY_ORDER.__getitem__,
            )
            async with self._factory() as uow:
                session = await uow.sessions.get(session_id, principal)
                selected = session.metadata.get("people_person_ids")
                if "people_person_ids" in session.metadata:
                    person_query = True
                    if (
                        isinstance(selected, list)
                        and 1 <= len(selected) <= 3
                        and all(isinstance(value, str) for value in selected)
                    ):
                        try:
                            explicit_ids = [UUID(value) for value in selected]
                        except ValueError:
                            explicit_ids = []
                        if len(set(explicit_ids)) == len(explicit_ids):
                            for person_id in explicit_ids:
                                person = await uow.people.get(
                                    principal, person_id, ceiling=ceiling, known_at=query.known_at
                                )
                                if isinstance(person, Person) and person.state != "merged":
                                    ids.append(person.id)
                else:
                    rows = await uow.people.query(
                        PeopleQuery(
                            tenant_id=principal.tenant_id,
                            principal_id=principal.principal_id,
                            sensitivity_ceiling=ceiling,
                            kinds=["person", "identifier"],
                            mentioned_in=query.text[:8192],
                            known_at=query.known_at,
                            as_of=query.as_of,
                            # Per-message alias copies count once (ADR-0118).
                            assigned="attached",
                            distinct_assignments=True,
                            limit=100,
                        )
                    )
                    # Overflow and collisions abstain; labels alone never authorize a merge.
                    if len(rows) <= 100:
                        matches: dict[str, set[UUID]] = {}
                        for row in rows:
                            if isinstance(row, Person) and row.state != "merged":
                                label, person_id = row.display_name, row.id
                            elif isinstance(row, PersonIdentifier) and row.person_id is not None:
                                instant = query.as_of or self._retriever.current_time()
                                if row.valid_from > instant or (
                                    row.valid_to is not None and row.valid_to <= instant
                                ):
                                    continue
                                if (
                                    row.identifier_kind == "role"
                                    and row.verification != "owner_confirmed"
                                ):
                                    continue
                                label, person_id = row.value, row.person_id
                            else:
                                continue
                            # A pronoun label would match nearly every request.
                            if is_non_person_reference(label):
                                continue
                            if re.search(
                                r"(?<!\w)" + re.escape(label) + r"(?!\w)", query.text, re.IGNORECASE
                            ):
                                matches.setdefault(label.casefold(), set()).add(person_id)
                        person_query = bool(matches)
                        ambiguous = (
                            set().union(*(values for values in matches.values() if len(values) > 1))
                            if matches
                            else set()
                        )
                        ids = (
                            sorted(
                                set().union(
                                    *(values for values in matches.values() if len(values) == 1)
                                )
                                - ambiguous
                            )[:3]
                            if matches
                            else []
                        )
        if ids:
            try:
                return await self.recall(
                    principal,
                    ids,
                    query,
                    session_id=session_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    moment=moment,
                    surface_id=surface_id,
                    measure_rendered_tokens=measure_rendered_tokens,
                    include_ordinary=True,
                )
            except NotFoundError:
                # Discovery can precede a concurrent erasure or privacy change.
                # The locked read revalidates every identity. If one vanished,
                # abstain from person context without interrupting ordinary Chat.
                person_query = True
        return await self._retriever.recall(
            query.model_copy(update={"people_scope": ()}) if person_query else query,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            moment=moment,
            surface_id=surface_id,
            measure_rendered_tokens=measure_rendered_tokens,
        )

    async def corrections(
        self, *, snapshot_id: UUID, watermark: int, as_of: datetime | None = None
    ) -> list[MemoryCorrection]:
        return await self._retriever.corrections(
            snapshot_id=snapshot_id, watermark=watermark, as_of=as_of
        )


class PeopleAwareMemoryRetriever:
    """In-turn adapter only; the session-open planner retains ordinary recall."""

    def __init__(self, service: PeopleContextService, principal: Principal) -> None:
        self._service = service
        self._principal = principal

    async def revision(self) -> int:
        return await self._service.revision(self._principal)

    async def recall(
        self,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
        measure_rendered_tokens: Callable[[str], int] | None = None,
    ) -> RecallResult:
        return await self._service.automatic_recall(
            self._principal,
            query,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            moment=moment,
            surface_id=surface_id,
            measure_rendered_tokens=measure_rendered_tokens,
        )

    async def corrections(
        self, *, snapshot_id: UUID, watermark: int, as_of: datetime | None = None
    ) -> list[MemoryCorrection]:
        return await self._service.corrections(
            snapshot_id=snapshot_id, watermark=watermark, as_of=as_of
        )
