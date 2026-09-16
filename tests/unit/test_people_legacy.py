"""Legacy linking preserves belief provenance and cannot guess or undo a repair."""

from datetime import timedelta
from uuid import uuid4

import pytest

from agent_core.application.people import PublicPeopleService
from agent_core.domain.errors import AuthorizationError, ConflictError
from agent_core.domain.events import NewEvent
from agent_core.domain.memory import MemoryAuthority, Sensitivity
from agent_core.domain.people import PeopleQuery, PeopleSource, PersonMemoryLink
from agent_core.domain.people_views import AddPersonAlias, CreatePerson, UpdatePerson
from agent_core.memory.people_legacy import link_existing_beliefs
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import memory
from tests.contract.support import NOW, memory_uow_factory, principal, session


@pytest.mark.parametrize(
    "failure",
    [None, "collision", "unrelated", "suppressed", "expired_alias", "external", "repaired"],
)
async def test_legacy_link_uses_only_its_cited_owner_evidence_and_preserves_belief(
    failure: str | None,
) -> None:
    clock, factory = await memory_uow_factory()
    await legacy_link_contract(factory, clock, failure)


async def legacy_link_contract(
    factory: UnitOfWorkFactory, clock: Clock, failure: str | None
) -> None:
    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "memory.read", "session.read"}}
    )
    service = PublicPeopleService(factory, clock)
    people = []
    for index in range(2 if failure in {"collision", "repaired"} else 1):
        person = await service.create(
            owner,
            CreatePerson(session_id=session().id, display_name=f"Alex {index}"),
            key=f"person-{index}",
            ceiling=Sensitivity.SENSITIVE,
        )
        await service.update(
            owner,
            person.id,
            UpdatePerson(
                session_id=session().id,
                expected_revision=1,
                alias=AddPersonAlias(
                    operation="add",
                    identifier_kind="name",
                    value="Alex" if index == 0 or failure == "collision" else "Taylor",
                    valid_from=NOW + timedelta(days=1)
                    if failure == "expired_alias"
                    else NOW - timedelta(days=30),
                ),
            ),
            key=f"alias-{index}",
            ceiling=Sensitivity.SENSITIVE,
        )
        people.append(person)
    async with factory() as uow:
        event = await uow.events.append(
            NewEvent(
                session_id=session().id,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id=owner.principal_id,
                payload={
                    "content": "Sam enjoys cycling."
                    if failure == "unrelated"
                    else "Alex enjoys cycling."
                },
            )
        )
        belief = memory().model_copy(
            update={
                "id": uuid4(),
                "subject": "Alex",
                "statement": "Alex enjoys cycling.",
                "source_event_ids": [event.sequence],
                "authority": MemoryAuthority.INFERRED
                if failure == "external"
                else MemoryAuthority.USER,
            }
        )
        belief = await uow.memories.upsert_belief(belief)
        original = belief.model_dump()
        if failure == "suppressed":
            from agent_core.domain.people_sources import source_id

            sid = source_id(owner, session().id, event.sequence)
            await uow.people.put(
                PeopleSource(
                    id=sid,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                    session_id=session().id,
                    event_sequence=event.sequence,
                    source_kind="owner",
                    evidence_at=event.created_at,
                    source_revision="test",
                ),
                expected_revision=0,
            )
            await uow.people.erase(owner, [sid])
        if failure == "repaired":
            await uow.people.put(
                PersonMemoryLink(
                    id=uuid4(),
                    person_id=people[1].id,
                    belief_id=belief.id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision=0,
            )
    result = await link_existing_beliefs(factory, clock, owner, limit=1)
    assert result.scanned == 1
    assert result.linked == int(failure is None)
    async with factory() as uow:
        assert (await uow.memories.get(belief.id, owner)).model_dump() == original
        links = await uow.people.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                kinds=["memory_link"],
                belief_id=belief.id,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
            )
        )
        if failure is None:
            assert len(links) == 1 and isinstance(links[0], PersonMemoryLink)
            assert links[0].person_id == people[0].id
            source = await uow.people.get(
                owner, links[0].support_ids[0], ceiling=Sensitivity.SENSITIVE
            )
            assert isinstance(source, PeopleSource) and source.evidence_at == event.created_at
        elif failure == "repaired":
            assert (
                len(links) == 1
                and isinstance(links[0], PersonMemoryLink)
                and links[0].person_id == people[1].id
            )
        else:
            assert links == []
    retry = await link_existing_beliefs(factory, clock, owner, limit=1)
    assert retry.linked == 0
    with pytest.raises(AuthorizationError):
        await link_existing_beliefs(factory, clock, owner.model_copy(update={"scopes": set()}))
    with pytest.raises(ConflictError):
        await link_existing_beliefs(factory, clock, owner, cursor="not-a-cursor")


async def test_legacy_link_cursor_is_bound_to_owner_and_belief_snapshot() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(
        update={"scopes": {"people.write", "memory.read", "session.read"}}
    )
    async with factory() as uow:
        for index in range(3):
            await uow.memories.upsert_belief(
                memory(belief_id=800 + index).model_copy(update={"store_position": index + 1})
            )
    first = await link_existing_beliefs(factory, clock, owner, limit=1)
    assert first.scanned == 1 and first.next_cursor
    second = await link_existing_beliefs(factory, clock, owner, limit=1, cursor=first.next_cursor)
    assert second.scanned == 1 and second.next_cursor
    last = await link_existing_beliefs(factory, clock, owner, limit=1, cursor=second.next_cursor)
    assert last.scanned == 1 and last.next_cursor is None
    with pytest.raises(ConflictError):
        await link_existing_beliefs(
            factory,
            clock,
            owner.model_copy(update={"principal_id": "other"}),
            limit=1,
            cursor=first.next_cursor,
        )
    with pytest.raises(ConflictError):
        await link_existing_beliefs(factory, clock, owner, limit=2, cursor=first.next_cursor)
    async with factory() as uow:
        await uow.memories.upsert_belief(
            memory(belief_id=900).model_copy(update={"store_position": 4})
        )
    with pytest.raises(ConflictError):
        await link_existing_beliefs(factory, clock, owner, limit=1, cursor=first.next_cursor)
