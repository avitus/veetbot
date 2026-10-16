"""Bounded dependency reads for People evidence retained between model calls."""

from datetime import datetime

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import LIVE_MEMORY_STATUSES, SENSITIVITY_ORDER, RecallTrace
from agent_core.domain.people import PeopleCommitment, PeopleQuery, Person, RelationshipAssertion
from agent_core.ports.persistence import RepositoryUnitOfWork


async def validate_people_dependencies(
    uow: RepositoryUnitOfWork, principal: Principal, trace: RecallTrace, *, now: datetime
) -> bool:
    """Called under the owner's erasure fence; never search or rank new facts.

    Revision equality conservatively invalidates the projection on any owner
    mutation. Explicit dependency reads also catch expiry as time advances,
    including a role-discovery edge that was not itself rendered.
    """
    dependencies = trace.people_dependencies
    if not (trace.people or trace.query.people_scope or dependencies):
        return True
    if "people.read" not in principal.scopes or dependencies is None:
        return False
    if (
        await uow.people.watermark(principal) != dependencies.people_revision
        or await uow.memories.head_position(principal) != dependencies.memory_revision
    ):
        return False
    query = trace.query
    at = query.as_of or now
    try:
        for person_id in query.people_scope or ():
            person = await uow.people.get(
                principal, person_id, ceiling=query.sensitivity_ceiling, known_at=query.known_at
            )
            if not isinstance(person, Person):
                return False
        for shown in trace.people:
            row = await uow.people.get(
                principal,
                shown.record_id,
                ceiling=query.sensitivity_ceiling,
                known_at=query.known_at,
            )
            if row is None or row.revision != shown.revision or row.kind != shown.kind:
                return False
            if isinstance(row, (RelationshipAssertion, PeopleCommitment)):
                if row.unresolved:
                    return False
                if isinstance(row, RelationshipAssertion) and (
                    (row.valid_from is not None and row.valid_from > at)
                    or (row.valid_to is not None and row.valid_to <= at)
                ):
                    return False
                if isinstance(row, PeopleCommitment) and row.state not in {
                    "open",
                    "proposed",
                    "uncertain",
                }:
                    return False
                belief = (
                    await uow.memories.get_at(row.belief_id, principal, known_at=query.known_at)
                    if query.known_at is not None
                    else await uow.memories.get(row.belief_id, principal)
                )
                if (
                    SENSITIVITY_ORDER[belief.sensitivity]
                    > SENSITIVITY_ORDER[query.sensitivity_ceiling]
                    or (query.as_of is None and belief.status not in LIVE_MEMORY_STATUSES)
                    or belief.valid_from > at
                    or (belief.valid_to is not None and belief.valid_to <= at)
                    or (belief.expires_at is not None and belief.expires_at <= at)
                ):
                    return False
        if dependencies.selection_belief_ids:
            people = await uow.people.query(
                PeopleQuery(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    sensitivity_ceiling=query.sensitivity_ceiling,
                    states=["active", "provisional"],
                    owner_relationship_belief_ids=list(dependencies.selection_belief_ids),
                    as_of=query.as_of,
                    known_at=query.known_at,
                    limit=1,
                )
            )
            if len(people) != 1 or tuple(row.id for row in people) != query.people_scope:
                return False
    except NotFoundError:
        return False
    return True
