"""Owner kinship repair (ADR-0258) shared by the in-memory and PostgreSQL suites."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from agent_core.domain.agents import Principal
from agent_core.domain.memory import BeliefType, MemoryAuthority, Polarity, Sensitivity
from agent_core.domain.people import (
    PeopleEndpoint,
    PeopleQuery,
    PeopleSource,
    Person,
    PersonMemoryLink,
    RelationshipAssertion,
)
from agent_core.domain.policies import TrustLevel
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.people_formation import source_id
from agent_core.memory.people_kinship import link_owner_kinship
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.support import ids


async def edges(factory: UnitOfWorkFactory, owner: Principal) -> list[RelationshipAssertion]:
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                kinds=["relationship"],
                limit=100,
            )
        )
    return [row for row in rows if isinstance(row, RelationshipAssertion)]


async def kinship_repair_case(
    factory: UnitOfWorkFactory,
    clock: Clock,
    owner: Principal,
    session_id: UUID,
    sequence: int,
) -> None:
    """`sequence` is the owner message "Erin is my wife. We married in 2005."."""

    owner = owner.model_copy(
        update={"scopes": {"people.read", "people.write", "memory.read", "session.read"}}
    )
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": clock.now(),
        "updated_at": clock.now(),
    }
    erin = Person(id=uuid4(), display_name="Erin", state="active", **common)
    earlier = Person(id=uuid4(), display_name="Erin Vitus", state="active", **common)
    async with factory() as uow:
        await uow.people.put(erin, expected_revision=0)
        await uow.people.put(earlier, expected_revision=0)
    service = GovernedMemoryService(factory, clock, ids(), owner, people_enabled=True)
    belief, _action = await service.remember_formation(
        session_id=session_id,
        run_id=None,
        statement="Andy Vitus says Erin is his wife and they married in 2005.",
        subject="person::Andy Vitus family: Erin",
        scope="veetbot",
        belief_type=BeliefType.RELATIONSHIP,
        portability=None,
        sensitivity=Sensitivity.SENSITIVE,
        source_event_ids=[sequence],
        origin_trust=TrustLevel.MEMORY,
        explicit=True,
        authority=MemoryAuthority.AFFIRMED,
        polarity=Polarity.ASSERT,
        confidence=None,
        valid_from=None,
        expires_at=None,
        trigger="explicit",
        record_audit=True,
    )
    sid = source_id(owner, session_id, sequence)
    supported = {**common, "sensitivity": Sensitivity.SENSITIVE}
    async with factory() as uow:
        await uow.people.put(
            PeopleSource(
                id=sid,
                session_id=session_id,
                event_sequence=sequence,
                source_kind="owner",
                evidence_at=clock.now(),
                source_revision="0" * 64,
                **supported,
            ),
            expected_revision=0,
        )
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                person_id=earlier.id,
                belief_id=belief.id,
                role="object",
                support_ids=[sid],
                **supported,
            ),
            expected_revision=0,
        )
        await uow.people.put(
            earlier.model_copy(update={"state": "merged", "merged_into": erin.id, "revision": 2}),
            expected_revision=1,
        )

    preview = await link_owner_kinship(factory, clock, owner, confirm=False)
    assert (preview.applied, preview.eligible, preview.pending, preview.created) == (
        False,
        1,
        1,
        0,
    )
    assert await edges(factory, owner) == []

    applied = await link_owner_kinship(factory, clock, owner, confirm=True)
    assert (applied.pending, applied.created) == (1, 1)
    [edge] = await edges(factory, owner)
    assert edge.predicate == "spouse"
    assert (edge.subject, edge.object) == (
        PeopleEndpoint(kind="owner"),
        PeopleEndpoint(kind="person", id=erin.id),
    )
    assert edge.belief_id == belief.id

    again = await link_owner_kinship(factory, clock, owner, confirm=True)
    assert (again.pending, again.created) == (0, 0)
    assert len(await edges(factory, owner)) == 1
