"""Owner kinship edges from explicit person-linked saves (ADR-0259)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.errors import AuthorizationError, NotFoundError
from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    BeliefType,
    MemoryAuthority,
    MemoryBrowseQuery,
    MemoryDerivation,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Sensitivity,
)
from agent_core.domain.people import (
    PeopleQuery,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    RelationshipAssertion,
)
from agent_core.domain.people_kinship import (
    OwnerKinship,
    kinship_endpoints,
    owner_kinship,
    statement_states_kinship,
)
from agent_core.domain.people_sources import source_id
from agent_core.domain.people_views import OwnerKinshipRepairReport
from agent_core.memory.communication_sources import FormationSourceKind, formation_source
from agent_core.ports.determinism import Clock
from agent_core.ports.people import PeopleStore
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

KINSHIP_LINK_VERSION = "owner-kinship@1"
_PAGE = 200
_EDGE_PAGE = 100
_MAX_PAGES = 50


def stated_kinship(belief: MemoryRecord, owner_text: str, identifier: str) -> OwnerKinship | None:
    """The tie a relationship save may record: the owner's words and the statement agree."""

    if belief.belief_type is not BeliefType.RELATIONSHIP:
        return None
    kinship = owner_kinship(owner_text, identifier)
    if kinship is None or not statement_states_kinship(belief.statement, kinship):
        return None
    return kinship


async def assert_owner_kinship(
    store: PeopleStore,
    principal: Principal,
    *,
    belief: MemoryRecord,
    person_id: UUID,
    kinship: OwnerKinship,
    support_ids: list[UUID],
    sensitivity: Sensitivity,
    now: datetime,
    dry_run: bool = False,
) -> bool:
    """Write the owner's tie once; an existing tie or an erased edge stays as it is.

    With `dry_run` nothing is written and the result says whether it would be.
    """

    edge_id = uuid5(belief.id, f"{KINSHIP_LINK_VERSION}:{person_id}")
    if await store.is_erased(principal, edge_id) or await store.get(
        principal, edge_id, ceiling=Sensitivity.RESTRICTED
    ):
        return False
    subject, object_ = kinship_endpoints(kinship, person_id)
    after: UUID | None = None
    for _page in range(_MAX_PAGES):
        existing = await store.query(
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kinds=["relationship"],
                person_id=person_id,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                sort="id",
                after=after,
                limit=_EDGE_PAGE,
            )
        )
        if any(
            isinstance(row, RelationshipAssertion)
            and not row.unresolved
            and row.valid_to is None
            and row.predicate == kinship[0]
            and (row.subject, row.object) == (subject, object_)
            for row in existing[:_EDGE_PAGE]
        ):
            return False
        if len(existing) <= _EDGE_PAGE:
            break
        after = existing[_EDGE_PAGE - 1].id
    if dry_run:
        return True
    await store.put(
        RelationshipAssertion(
            id=edge_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            created_at=now,
            updated_at=now,
            sensitivity=sensitivity,
            support_ids=support_ids,
            subject=subject,
            object=object_,
            predicate=kinship[0],
            belief_id=belief.id,
        ),
        expected_revision=0,
    )
    return True


async def _identity(store: PeopleStore, principal: Principal, person_id: UUID) -> Person | None:
    """Follow merge redirects to the surviving person, within a few hops."""

    for _hop in range(5):
        row = await store.get(principal, person_id, ceiling=Sensitivity.RESTRICTED)
        if not isinstance(row, Person):
            return None
        if row.state != "merged" or row.merged_into is None:
            return row
        person_id = row.merged_into
    return None


async def _names(store: PeopleStore, principal: Principal, *people: Person) -> list[str]:
    """Display names plus the owner-confirmed name aliases of the survivor."""

    names = {person.display_name for person in people}
    aliases = await store.query(
        PeopleQuery(
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            kinds=["identifier"],
            person_id=people[-1].id,
            sensitivity_ceiling=Sensitivity.RESTRICTED,
            limit=100,
        )
    )
    names.update(
        alias.value
        for alias in aliases
        if isinstance(alias, PersonIdentifier)
        and alias.identifier_kind == "name"
        and alias.verification == "owner_confirmed"
    )
    return sorted(names)


async def _owner_text(
    uow: RepositoryUnitOfWork, principal: Principal, belief: MemoryRecord, sequence: int
) -> tuple[UUID, str] | None:
    sid = source_id(principal, belief.source_session_id, sequence)
    if await uow.people.source_suppressed(principal, sid):
        return None
    try:
        rows = await uow.events.list_after(
            belief.source_session_id, sequence - 1, principal, limit=1
        )
    except NotFoundError:
        return None
    if not rows or rows[0].sequence != sequence:
        return None
    source = formation_source(rows[0], principal)
    if source is None or source.kind is not FormationSourceKind.OWNER_ASSERTION:
        return None
    return sid, source.text


async def link_owner_kinship(
    factory: UnitOfWorkFactory, clock: Clock, principal: Principal, *, confirm: bool
) -> OwnerKinshipRepairReport:
    """Record ties already stated in owner messages behind earlier linked saves.

    Only live, direct, owner-confirmed relationship memories linked to People
    qualify, and only when their own cited owner message states the tie; the
    edge goes to the surviving identity when the linked person was merged.
    Without `confirm` nothing is written.
    """

    if not {"people.write", "memory.read", "session.read"} <= principal.scopes:
        raise AuthorizationError(
            "kinship linking requires people.write, memory.read, and session.read"
        )
    scanned = eligible = pending = created = 0
    # A tie restated by several saves is one edge; the preview counts it once.
    planned: set[tuple[UUID, OwnerKinship]] = set()
    async with factory() as uow, uow.people.lock(principal):
        cursor: tuple[int, UUID] | None = None
        for _page in range(_MAX_PAGES):
            page = await uow.memories.browse(
                MemoryBrowseQuery(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    ceiling=Sensitivity.RESTRICTED,
                    statuses=(MemoryStatus.ACTIVE,),
                    belief_types=(BeliefType.RELATIONSHIP,),
                    limit=_PAGE,
                    cursor=cursor,
                )
            )
            for belief in page[:_PAGE]:
                scanned += 1
                if (
                    belief.authority not in {MemoryAuthority.USER, MemoryAuthority.AFFIRMED}
                    or belief.derivation is not MemoryDerivation.DIRECT
                    or belief.polarity is not Polarity.ASSERT
                    or not 1 <= len(belief.source_event_ids) <= 32
                ):
                    continue
                links = await uow.people.query(
                    PeopleQuery(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        kinds=["memory_link"],
                        belief_id=belief.id,
                        sensitivity_ceiling=Sensitivity.RESTRICTED,
                        limit=6,
                    )
                )
                linked = [
                    row
                    for row in links[:6]
                    if isinstance(row, PersonMemoryLink) and not row.unresolved
                ]
                if not linked:
                    continue
                eligible += 1
                sources = [
                    found
                    for sequence in sorted(set(belief.source_event_ids))
                    if (found := await _owner_text(uow, principal, belief, sequence)) is not None
                ]
                for link in linked:
                    original = await uow.people.get(
                        principal, link.person_id, ceiling=Sensitivity.RESTRICTED
                    )
                    survivor = await _identity(uow.people, principal, link.person_id)
                    if not isinstance(original, Person) or survivor is None:
                        continue
                    names = await _names(uow.people, principal, original, survivor)
                    for sid, text in sources:
                        kinships = {
                            kinship
                            for name in names
                            if (kinship := stated_kinship(belief, text, name)) is not None
                        }
                        if len(kinships) != 1:
                            continue
                        (kinship,) = kinships
                        if (survivor.id, kinship) in planned:
                            break
                        sensitivity = max(
                            (belief.sensitivity, survivor.sensitivity, Sensitivity.SENSITIVE),
                            key=SENSITIVITY_ORDER.__getitem__,
                        )
                        for dry_run in (True, False):
                            if not dry_run and not confirm:
                                break
                            written = await assert_owner_kinship(
                                uow.people,
                                principal,
                                belief=belief,
                                person_id=survivor.id,
                                kinship=kinship,
                                support_ids=[sid],
                                sensitivity=sensitivity,
                                now=clock.now(),
                                dry_run=dry_run,
                            )
                            if not written:
                                break
                            if dry_run:
                                pending += 1
                                planned.add((survivor.id, kinship))
                            else:
                                created += 1
                        break
            if len(page) <= _PAGE:
                break
            last = page[_PAGE - 1]
            cursor = (last.store_position, last.id)
    return OwnerKinshipRepairReport(
        applied=confirm, scanned=scanned, eligible=eligible, pending=pending, created=created
    )
