"""Owner-intent People links around the existing governed memory writer."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ToolTrustRejectedError,
    ToolValidationError,
)
from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    MemoryAuthority,
    MemoryRecord,
    Polarity,
    Sensitivity,
)
from agent_core.domain.people import (
    PeopleQuery,
    PeopleSource,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    normalize_identifier,
)
from agent_core.domain.people_tools import RememberPeopleArgs
from agent_core.domain.policies import TrustLevel
from agent_core.memory.communication_sources import FormationSourceKind, formation_source
from agent_core.memory.people import identifier_occurs
from agent_core.memory.people_formation import source_id
from agent_core.ports.determinism import Clock
from agent_core.ports.people import PeopleStore
from agent_core.ports.persistence import UnitOfWorkFactory

if TYPE_CHECKING:
    from agent_core.memory.formation import GovernedMemoryService


async def _owner_identifies(
    store: PeopleStore, principal: Principal, person: Person, text: str, at: datetime
) -> bool:
    query = PeopleQuery(
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=100,
    )
    aliases = await store.query(
        query.model_copy(update={"kinds": ["identifier"], "person_id": person.id})
    )
    if len(aliases) > 100:
        return False
    identifiers = [("name", "owner", "owner", person.display_name)] + [
        (alias.identifier_kind, alias.namespace, alias.context, alias.value)
        for alias in aliases
        if isinstance(alias, PersonIdentifier)
        and alias.verification == "owner_confirmed"
        and alias.context in {"", "owner"}
        and alias.valid_from <= at
        and (alias.valid_to is None or at < alias.valid_to)
    ]
    for kind, namespace, context, value in identifiers:
        if not identifier_occurs(text, value, kind):
            continue
        rows = await store.query(
            query.model_copy(update={"kinds": ["person", "identifier"], "text": value})
        )
        if len(rows) > 100:
            continue
        normalized = normalize_identifier(kind, namespace, value)
        matches = set()
        for row in rows:
            if isinstance(row, Person) and kind == "name" and row.state != "merged":
                if normalize_identifier(kind, namespace, row.display_name) == normalized:
                    matches.add(row.id)
            elif (
                isinstance(row, PersonIdentifier)
                and row.person_id is not None
                and row.identifier_kind == kind
                and row.namespace == namespace
                and row.context == context
                and normalize_identifier(kind, namespace, row.value) == normalized
                and row.valid_from <= at
                and (row.valid_to is None or at < row.valid_to)
            ):
                matches.add(row.person_id)
        if matches == {person.id}:
            return True
    return False


async def remember_linked(
    service: GovernedMemoryService,
    factory: UnitOfWorkFactory,
    clock: Clock,
    principal: Principal,
    arguments: RememberPeopleArgs,
    *,
    session_id: UUID,
    run_id: UUID | None,
    origin_trust: TrustLevel,
) -> MemoryRecord:
    if "people.write" not in principal.scopes:
        raise AuthorizationError("missing required scope: people.write")
    # Recalled memory in context is memory trust, as for any explicit remember;
    # owner intent is the owner message below, which must name every person.
    if origin_trust not in {TrustLevel.USER, TrustLevel.MEMORY}:
        raise ToolTrustRejectedError("person-linked explicit memory requires owner intent")
    refs = arguments.person_refs
    if not refs or len({ref.person_id for ref in refs}) != len(refs):
        raise ToolValidationError("person references must be nonempty and distinct")
    async with factory() as uow, uow.people.lock(principal):
        await uow.sessions.get(session_id, principal)
        event = await uow.events.latest_before(
            session_id, 2**63 - 1, "user.message.created", principal
        )
        source = formation_source(event, principal) if event is not None else None
        if source is None or source.kind is not FormationSourceKind.OWNER_ASSERTION:
            raise ToolValidationError("person-linked memory requires an owner source")
        sid = source_id(principal, session_id, source.event.sequence)
        if await uow.people.source_suppressed(principal, sid):
            raise ConflictError("People source was erased")
        sensitivity = max(
            (arguments.sensitivity, Sensitivity.SENSITIVE), key=SENSITIVITY_ORDER.__getitem__
        )
        for ref in refs:
            person = await uow.people.get(principal, ref.person_id, ceiling=Sensitivity.SENSITIVE)
            if not isinstance(person, Person):
                raise NotFoundError("person not found")
            if person.revision != ref.expected_revision or person.state == "merged":
                raise ConflictError("person revision changed")
            if not await _owner_identifies(
                uow.people, principal, person, source.text, source.event.created_at
            ):
                raise ToolValidationError("owner source does not identify the selected person")
            sensitivity = max((sensitivity, person.sensitivity), key=SENSITIVITY_ORDER.__getitem__)
        subject_people = sorted(str(ref.person_id) for ref in refs if ref.role == "subject")
        stable_subject = f"person:{','.join(subject_people)}:{arguments.subject}"
        if len(stable_subject) > 512:
            raise ToolValidationError("person-linked subject exceeds the bound")
        belief, _action = await service.remember_formation(
            session_id=session_id,
            run_id=run_id,
            statement=arguments.statement,
            subject=stable_subject,
            scope=arguments.scope,
            belief_type=arguments.belief_type,
            portability=arguments.portability,
            sensitivity=sensitivity,
            source_event_ids=[source.event.sequence],
            origin_trust=origin_trust,
            explicit=True,
            authority=(
                MemoryAuthority.AFFIRMED
                if origin_trust is TrustLevel.MEMORY
                else MemoryAuthority.USER
            ),
            polarity=Polarity.ASSERT,
            confidence=None,
            valid_from=None,
            expires_at=None,
            trigger="explicit",
            record_audit=True,
            existing_uow=uow,
        )
        if await uow.people.get(principal, sid, ceiling=Sensitivity.RESTRICTED) is None:
            await uow.people.put(
                PeopleSource(
                    id=sid,
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    created_at=clock.now(),
                    updated_at=clock.now(),
                    sensitivity=sensitivity,
                    session_id=session_id,
                    event_sequence=source.event.sequence,
                    source_kind="owner",
                    evidence_at=source.event.created_at,
                    source_revision=hashlib.sha256(
                        source.event.model_dump_json().encode()
                    ).hexdigest(),
                ),
                expected_revision=0,
            )
        for ref in refs:
            link_id = uuid5(belief.id, f"{ref.person_id}:{ref.role}")
            if await uow.people.get(principal, link_id, ceiling=Sensitivity.RESTRICTED) is None:
                await uow.people.put(
                    PersonMemoryLink(
                        id=link_id,
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        created_at=clock.now(),
                        updated_at=clock.now(),
                        sensitivity=sensitivity,
                        support_ids=[sid],
                        person_id=ref.person_id,
                        belief_id=belief.id,
                        role=ref.role,
                    ),
                    expected_revision=0,
                )
        return belief
