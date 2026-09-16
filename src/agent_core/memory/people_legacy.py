"""Bounded deterministic links from existing beliefs and their own cited sources."""

from __future__ import annotations

import base64
import hashlib
import json
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ToolValidationError,
)
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    MemoryAuthority,
    MemoryBrowseQuery,
    MemoryDerivation,
    MemoryRecord,
    Sensitivity,
)
from agent_core.domain.people import PeopleQuery, PeopleSource, Person, PersonMemoryLink
from agent_core.domain.people_sources import identifier_occurs, source_id
from agent_core.domain.people_views import LegacyPeopleLinkResult
from agent_core.memory.communication_sources import FormationSourceKind, formation_source
from agent_core.memory.people import resolve_identity
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


def _safe(text: str) -> bool:
    return not contains_injection_pattern(text) and not contains_secret_material(text)


async def _link(
    uow: RepositoryUnitOfWork, clock: Clock, owner: Principal, belief: MemoryRecord
) -> bool:
    now = clock.now()
    if (
        belief.authority not in {MemoryAuthority.USER, MemoryAuthority.AFFIRMED}
        or belief.derivation != MemoryDerivation.DIRECT
        or belief.valid_from > now
        or (belief.valid_to is not None and belief.valid_to <= now)
        or (belief.expires_at is not None and belief.expires_at <= now)
        or not 1 <= len(belief.source_event_ids) <= 32
        or not _safe(belief.statement)
        or not _safe(belief.subject)
    ):
        return False
    link_id = uuid5(belief.id, "people-legacy@1:subject")
    if await uow.people.is_erased(owner, link_id):
        return False
    query = PeopleQuery(
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        kinds=["memory_link"],
        belief_id=belief.id,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        limit=1,
    )
    if await uow.people.query(query):
        # Includes unresolved and repaired assignments. Linking never overrides them.
        return False
    sources: list[PeopleSource] = []
    person_id: UUID | None = None
    for sequence in sorted(set(belief.source_event_ids)):
        sid = source_id(owner, belief.source_session_id, sequence)
        if await uow.people.source_suppressed(owner, sid):
            return False
        try:
            rows = await uow.events.list_after(
                belief.source_session_id, sequence - 1, owner, limit=1
            )
        except NotFoundError:
            return False
        if not rows or rows[0].sequence != sequence:
            return False
        source = formation_source(rows[0], owner)
        if (
            source is None
            or source.kind != FormationSourceKind.OWNER_ASSERTION
            or not _safe(source.text)
            or not identifier_occurs(source.text, belief.subject, "name")
        ):
            return False
        resolution = await resolve_identity(
            uow.people,
            owner,
            kind="name",
            namespace="owner",
            value=belief.subject,
            context="owner",
            at=rows[0].created_at,
            ceiling=Sensitivity.RESTRICTED,
        )
        if resolution.status != "matched" or len(resolution.person_ids) != 1:
            return False
        if person_id is not None and person_id != resolution.person_ids[0]:
            return False
        person_id = resolution.person_ids[0]
        person = await uow.people.get(owner, person_id, ceiling=Sensitivity.SENSITIVE)
        if not isinstance(person, Person) or person.state == "merged":
            return False
        sensitivity = max(
            (Sensitivity.SENSITIVE, belief.sensitivity, person.sensitivity),
            key=SENSITIVITY_ORDER.__getitem__,
        )
        sources.append(
            PeopleSource(
                id=sid,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=now,
                updated_at=now,
                sensitivity=sensitivity,
                session_id=belief.source_session_id,
                event_sequence=sequence,
                source_kind="owner",
                evidence_at=rows[0].created_at,
                source_revision=hashlib.sha256(rows[0].model_dump_json().encode()).hexdigest(),
            )
        )
    if person_id is None:
        return False
    for row in sources:
        if await uow.people.get(owner, row.id, ceiling=Sensitivity.RESTRICTED) is None:
            await uow.people.put(row, expected_revision=0)
    await uow.people.put(
        PersonMemoryLink(
            id=link_id,
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            created_at=now,
            updated_at=now,
            sensitivity=sensitivity,
            person_id=person_id,
            belief_id=belief.id,
            support_ids=[row.id for row in sources],
            role="subject",
        ),
        expected_revision=0,
    )
    return True


async def link_existing_beliefs(
    factory: UnitOfWorkFactory,
    clock: Clock,
    principal: Principal,
    *,
    limit: int = 100,
    cursor: str | None = None,
) -> LegacyPeopleLinkResult:
    if not {"people.write", "memory.read", "session.read"} <= principal.scopes:
        raise AuthorizationError(
            "legacy linking requires people.write, memory.read, and session.read"
        )
    if not 1 <= limit <= 100 or (cursor is not None and len(cursor) > 2048):
        raise ToolValidationError(
            "legacy linking requires a limit from 1 to 100 and a bounded cursor"
        )
    binding = hashlib.sha256(
        json.dumps([principal.tenant_id, principal.principal_id, limit]).encode()
    ).hexdigest()
    position = None
    expected_head = None
    if cursor is not None:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor))
            if decoded["binding"] != binding:
                raise ValueError("cursor binding")
            position = (int(decoded["position"]), UUID(decoded["id"]))
            expected_head = decoded["head"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ConflictError("legacy linking cursor is invalid; restart the scan") from exc
    async with factory() as uow, uow.people.lock(principal):
        head = await uow.memories.head_position(principal)
        if expected_head is not None and head != expected_head:
            raise ConflictError("beliefs changed; restart legacy linking")
        records = await uow.memories.browse(
            MemoryBrowseQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                ceiling=Sensitivity.SENSITIVE,
                limit=limit,
                cursor=position,
            )
        )
        linked = 0
        for belief in records[:limit]:
            linked += int(await _link(uow, clock, principal, belief))
        next_cursor = None
        if len(records) > limit:
            last = records[limit - 1]
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "binding": binding,
                        "head": head,
                        "position": last.store_position,
                        "id": str(last.id),
                    }
                ).encode()
            ).decode()
        return LegacyPeopleLinkResult(
            scanned=min(len(records), limit),
            linked=linked,
            unchanged=min(len(records), limit) - linked,
            next_cursor=next_cursor,
        )
