"""Local identity resolution; personal directories never enter extraction prompts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import (
    PeopleQuery,
    PeopleValue,
    Person,
    PersonIdentifier,
    normalize_identifier,
)
from agent_core.domain.people_sources import identifier_occurs as identifier_occurs
from agent_core.ports.people import PeopleStore


class IdentityResolution(PeopleValue):
    status: Literal["matched", "ambiguous", "unresolved"]
    person_ids: list[UUID]


async def resolve_identity(
    store: PeopleStore,
    principal: Principal,
    *,
    kind: str,
    namespace: str,
    value: str,
    context: str,
    at: datetime,
    ceiling: Sensitivity,
) -> IdentityResolution:
    normalized = normalize_identifier(kind, namespace, value)
    # Exact values only, one row per distinct assignment (ADR-0118): per-message
    # identifier copies and superstring names can no longer fill the bounded
    # candidate set and turn a known person ambiguous.
    query = PeopleQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        kinds=["identifier", "person"] if kind == "name" else ["identifier"],
        identifier_value=normalized,
        assigned="attached",
        valid_at=at,
        distinct_assignments=True,
        sensitivity_ceiling=ceiling,
        limit=100,
    )
    rows = await store.query(query)
    matches: set[UUID] = set()
    qualified: set[UUID] = set()
    for row in rows:
        if isinstance(row, Person):
            if (
                row.state != "merged"
                and normalize_identifier("name", "owner", row.display_name) == normalized
            ):
                matches.add(row.id)
        elif isinstance(row, PersonIdentifier) and row.person_id:
            if (
                row.identifier_kind != kind
                or row.namespace != namespace
                or normalize_identifier(kind, namespace, row.value) != normalized
                or row.valid_from > at
                or (row.valid_to is not None and at >= row.valid_to)
                or row.context != context
            ):
                continue
            matches.add(row.person_id)
            if (
                row.verification == "owner_confirmed"
                or (row.verification == "channel_observed" and kind in {"email", "phone", "handle"})
                or (row.verification == "contextual" and kind == "name" and bool(context))
                # An address or number the owner stated in chat (context "owner")
                # identifies that person for mail and texts (ADR-0118).
                or (
                    row.verification == "contextual"
                    and kind in {"email", "phone"}
                    and row.context == "owner"
                )
            ):
                qualified.add(row.person_id)
    # An evidenced, specific context can select one alias among unqualified
    # display-name candidates. Generic owner context supplies no such distinction.
    if len(qualified) == 1 and (context not in {"", "owner"} or len(matches) == 1):
        matches = qualified
    ordered = sorted(matches)
    # A truncated collision set cannot be promoted to a match. A name alone
    # supplies candidates; the owner or source context resolves identity.
    status: Literal["matched", "ambiguous", "unresolved"] = "unresolved"
    if len(rows) > query.limit or len(matches) > 1:
        status = "ambiguous"
    elif len(matches) == 1:
        status = "matched" if matches == qualified else "ambiguous"
    return IdentityResolution(status=status, person_ids=ordered[:20])
