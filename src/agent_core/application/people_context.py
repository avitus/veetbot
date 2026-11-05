"""Task-sized People recall using the governed belief ranker and shared budget."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError, ToolValidationError
from agent_core.domain.hazards import is_safe_text
from agent_core.domain.memory import (
    LIVE_MEMORY_STATUSES,
    SENSITIVITY_ORDER,
    BeliefType,
    MemoryCorrection,
    Polarity,
    RecallQuery,
    RecallResult,
    Sensitivity,
    TracedPersonContext,
)
from agent_core.domain.memory_candidates import candidate_strength
from agent_core.domain.messages import MemoryRecallUsage
from agent_core.domain.people import (
    OWNER_RELATIONSHIP_GROUPS,
    OrganizationReference,
    PeopleCommitment,
    PeopleInteraction,
    PeopleQuery,
    PeopleRelationshipFilter,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    RelationshipAssertion,
    is_group_or_service_name,
    is_non_person_reference,
    referenced_people,
)
from agent_core.domain.recall_execution import recall_search
from agent_core.ports.people_runtime import PeopleRecall
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

# How close an owner relationship is, for overview paging: lower is closer.
_RANK = ("partner", "child", "parent", "sibling", "relative")
# Bounds one overview's identity reads per relationship group; a larger group
# is named as incomplete rather than paged (ADR-0259).
_OVERVIEW_CANDIDATES = 50
# Focal people per page (ADR-0238), and the most a first page may hold while it
# reaches the first member of every family tier present (ADR-0265).
_PAGE_FOCAL = 3
_FIRST_PAGE_FOCAL = 8


def _closeness(edge: RelationshipAssertion) -> int:
    owner_first = edge.subject.kind == "owner"
    role = {
        "spouse": "partner",
        "partner": "partner",
        # owner --parent--> X makes X a child; X --parent--> owner a parent.
        "parent": "child" if owner_first else "parent",
        "child": "parent" if owner_first else "child",
        "sibling": "sibling",
    }.get(edge.predicate, "relative")
    return _RANK.index(role)


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
        relationship_discovery: bool = False,
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
        bound_semantic_queries = await self._retriever.bind_semantic_queries(
            query, session_id=session_id, run_id=run_id, surface_id=surface_id
        )
        belief_ids: set[UUID] = set()
        context: list[TracedPersonContext] = []
        neighbors: set[UUID] = set()
        projected: set[UUID] = set()
        async with self._factory() as uow, uow.people.lock(principal):
            await uow.sessions.get(session_id, principal)
            selection_belief_ids: tuple[UUID, ...] = ()
            if relationship_discovery:
                current, _, selection_belief_ids = await self._relationship_people(
                    uow, principal, query, ceiling
                )
                if current != person_ids:
                    raise NotFoundError("relationship identity changed")
            for person_id in person_ids:
                person = await uow.people.get(
                    principal, person_id, ceiling=ceiling, known_at=query.known_at
                )
                if not isinstance(person, Person) or not is_safe_text(person.display_name):
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
                    if not isinstance(row, PeopleInteraction) or not is_safe_text(row.summary):
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
                    else tuple(
                        sorted(
                            belief_ids
                            if query.include_ids is None
                            else belief_ids & set(query.include_ids)
                        )[:1000]
                    ),
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
                people_selection_belief_ids=selection_belief_ids,
                bound_semantic_queries=bound_semantic_queries,
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
            or not is_safe_text(belief.statement)
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
                if not isinstance(entity, (Person, OrganizationReference)) or not is_safe_text(
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
        if not is_safe_text(detail):
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
        relationship_discovery = False
        overview = query.relationship_group is not None or (
            query.candidate_selection == "relevance"
            and bool(
                re.search(
                    r"\b(?:everything (?:you )?(?:know|remember) about me|"
                    r"what (?:do you |you )?(?:know|remember) about me|"
                    r"(?:tell me|what (?:do you |you )?(?:know|remember)).*\bmy family)\b",
                    query.text or "",
                    re.IGNORECASE,
                )
            )
        )
        if query.relationship_group is not None:
            require_scope(principal, "people.read")
        if overview and "people.read" in principal.scopes and moment == "in_turn":
            return await self._owner_overview(
                principal,
                query,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                moment=moment,
                surface_id=surface_id,
                measure_rendered_tokens=measure_rendered_tokens,
            )
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
                            # Per-message alias copies count once (ADR-0121).
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
                            if is_non_person_reference(label) or is_group_or_service_name(label):
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
                        if not person_query and query.candidate_selection == "relevance":
                            ids, person_query, _ = await self._relationship_people(
                                uow, principal, query, ceiling
                            )
                            relationship_discovery = bool(ids)
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
                    relationship_discovery=relationship_discovery,
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

    async def _owner_overview(
        self,
        principal: Principal,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None,
        turn_id: UUID | None,
        moment: str,
        surface_id: str,
        measure_rendered_tokens: Callable[[str], int] | None,
    ) -> RecallResult:
        require_scope(principal, "people.read")
        if (principal.tenant_id, principal.principal_id) != (query.tenant_id, query.principal_id):
            raise NotFoundError("person not found")
        ceiling = min(
            (query.sensitivity_ceiling, self._surface_ceiling), key=SENSITIVITY_ORDER.__getitem__
        )
        group = query.relationship_group or "family"
        groups: tuple[PeopleRelationshipFilter, ...] = (
            ("family", "partner") if group == "family" else (group,)
        )
        predicates = {predicate for key in groups for predicate in OWNER_RELATIONSHIP_GROUPS[key]}
        context: list[TracedPersonContext] = []
        evidence: set[UUID] = set()
        bound_semantic_queries = await self._retriever.bind_semantic_queries(
            query, session_id=session_id, run_id=run_id, surface_id=surface_id
        )
        async with self._factory() as uow, uow.people.lock(principal):
            await uow.sessions.get(session_id, principal)
            people: dict[UUID, Person] = {}
            overflow = False
            for key in groups:
                page = await uow.people.query(
                    PeopleQuery(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        relationship=key,
                        sensitivity_ceiling=ceiling,
                        states=["active", "provisional"],
                        as_of=query.as_of,
                        known_at=query.known_at,
                        limit=_OVERVIEW_CANDIDATES,
                    )
                )
                # The repository returns one row past the limit when more exist.
                overflow = overflow or len(page) > _OVERVIEW_CANDIDATES
                people.update(
                    (row.id, row) for row in page[:_OVERVIEW_CANDIDATES] if isinstance(row, Person)
                )
            owner_edges: dict[UUID, list[RelationshipAssertion]] = {}
            for person_id in people:
                rows = await uow.people.query(
                    PeopleQuery(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        kinds=["relationship"],
                        person_id=person_id,
                        sensitivity_ceiling=ceiling,
                        as_of=query.as_of,
                        known_at=query.known_at,
                        limit=100,
                    )
                )
                owner_edges[person_id] = [
                    row
                    for row in rows[:100]
                    if isinstance(row, RelationshipAssertion)
                    and not row.unresolved
                    and row.predicate in predicates
                    and any(endpoint.kind == "owner" for endpoint in (row.subject, row.object))
                ]
            # ADR-0259: pages run closest first (partner, children, parents,
            # siblings, other relatives), so the automatic first page holds the
            # owner's household rather than whoever has the lowest identifier.
            ordered = sorted(
                people,
                key=lambda key: (
                    min((_closeness(edge) for edge in owner_edges[key]), default=len(_RANK)),
                    key,
                ),
            )
            if query.people_after is not None:
                ordered = (
                    ordered[ordered.index(query.people_after) + 1 :]
                    if query.people_after in ordered
                    else [key for key in ordered if key > query.people_after]
                )
            # ADR-0265: a first page runs, closest first, until it has reached the
            # first member of every tier present, so a parent or sibling is not left
            # on a page the model never asks for; continuation pages hold three.
            size = _PAGE_FOCAL
            if query.people_after is None:
                tiers: set[int] = set()
                for index, focal in enumerate(ordered[:_FIRST_PAGE_FOCAL]):
                    tier = min(
                        (_closeness(edge) for edge in owner_edges[focal]), default=len(_RANK)
                    )
                    if tier not in tiers:
                        tiers.add(tier)
                        size = max(size, index + 1)
            next_after = ordered[size - 1] if len(ordered) > size else None
            for person_id in ordered[:size]:
                person = people[person_id]
                if not is_safe_text(person.display_name):
                    continue
                edges = owner_edges[person_id]
                # Exact linked IDs still pass every ordinary memory visibility predicate.
                eligible = await uow.memories.query(
                    query.model_copy(
                        update={
                            "text": None,
                            "subjects": [],
                            "structured_belief_types": [],
                            "include_owned_resources": False,
                            "include_home_location": False,
                            "expand_ids": (),
                            "include_ids": tuple(
                                sorted(
                                    {edge.belief_id for edge in edges}
                                    & (
                                        set(query.include_ids)
                                        if query.include_ids is not None
                                        else {edge.belief_id for edge in edges}
                                    )
                                )
                            ),
                            "sensitivity_ceiling": ceiling,
                            "max_items": 20,
                        }
                    )
                )
                permitted = {row.id: row for row in eligible if row.polarity is Polarity.ASSERT}
                for edge in edges:
                    belief = permitted.get(edge.belief_id)
                    if belief is None:
                        continue
                    shown = await self._relationship_context(
                        uow, principal, edge, query, ceiling, {person_id}, set()
                    )
                    if shown is not None:
                        context.append(
                            shown.model_copy(
                                update={
                                    "text": shown.text + f" Evidence: {belief.authority.value}; "
                                    f"{belief.status.value}; confidence {belief.confidence:g}."
                                }
                            )
                        )
                        evidence.add(belief.id)
            effective = query.model_copy(
                update={
                    "relationship_group": group,
                    "next_people_after": next_after,
                    "people_overflow": overflow,
                    "expand_ids": tuple(sorted(evidence)),
                    "sensitivity_ceiling": ceiling,
                    "budget_tokens": min(query.budget_tokens, 2000),
                    "max_items": min(query.max_items, 20),
                }
            )
            return await self._retriever.recall(
                effective,
                session_id=session_id,
                run_id=run_id,
                turn_id=turn_id,
                moment=moment,
                surface_id=surface_id,
                people_items=context,
                existing_uow=uow,
                bound_semantic_queries=bound_semantic_queries,
                measure_rendered_tokens=measure_rendered_tokens,
            )

    async def _relationship_people(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        query: RecallQuery,
        ceiling: Sensitivity,
    ) -> tuple[list[UUID], bool, tuple[UUID, ...]]:
        # This is identity discovery, not a hard type filter on the final facts.
        # Keep every visibility restriction and use ordinary bounded lexical
        # selection; no inferred role vocabulary or extra encoder call.
        discovery = query.model_copy(
            update={
                "belief_types": [BeliefType.RELATIONSHIP],
                "structured_belief_types": [],
                "include_owned_resources": False,
                "include_home_location": False,
                "expand_ids": (),
                "subjects": [],
                "sensitivity_ceiling": ceiling,
                "max_items": min(query.max_items, 20),
            }
        )
        records = await uow.memories.query(discovery)
        evidence = [row.id for row in records if candidate_strength(row, discovery)[1] > 0]
        if not evidence:
            return [], False, ()
        people = await uow.people.query(
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                sensitivity_ceiling=ceiling,
                states=["active", "provisional"],
                owner_relationship_belief_ids=evidence,
                as_of=query.as_of,
                known_at=query.known_at,
                limit=1,  # The repository returns one additional row for ambiguity.
            )
        )
        if (
            len(people) == 1
            and isinstance(people[0], Person)
            and is_safe_text(people[0].display_name)
        ):
            return [people[0].id], True, tuple(evidence)
        return [], bool(people), ()

    async def validate_retained(
        self, usage: MemoryRecallUsage, *, session_id: UUID, run_id: UUID
    ) -> bool:
        validator = getattr(self._retriever, "validate_retained", None)
        return bool(
            callable(validator) and await validator(usage, session_id=session_id, run_id=run_id)
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
        async with recall_search(query, session_id=session_id, run_id=run_id):
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

    async def validate_retained(
        self, usage: MemoryRecallUsage, *, session_id: UUID, run_id: UUID
    ) -> bool:
        return await self._service.validate_retained(usage, session_id=session_id, run_id=run_id)

    async def corrections(
        self, *, snapshot_id: UUID, watermark: int, as_of: datetime | None = None
    ) -> list[MemoryCorrection]:
        return await self._service.corrections(
            snapshot_id=snapshot_id, watermark=watermark, as_of=as_of
        )
