"""Public People writes and reads preserve owner scope and revisions."""

from typing import Literal

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.application.people import PublicPeopleService
from agent_core.domain.agents import Principal
from agent_core.domain.errors import AuthorizationError, ConflictError, NotFoundError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleEndpoint
from agent_core.domain.people_views import CreatePerson, UpdatePerson
from agent_core.domain.views import MemoryView
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import memory_uow_factory, principal, session


async def test_people_management_is_idempotent_and_scope_bound() -> None:
    clock, factory = await memory_uow_factory()
    service = PublicPeopleService(factory, clock)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    request = CreatePerson(session_id=session().id, display_name="Alex")
    with pytest.raises(AuthorizationError):
        await service.create(principal(), request, key="create-1", ceiling=Sensitivity.SENSITIVE)
    person = await service.create(owner, request, key="create-1", ceiling=Sensitivity.SENSITIVE)
    assert (
        await service.create(owner, request, key="create-1", ceiling=Sensitivity.SENSITIVE)
        == person
    )
    with pytest.raises(ConflictError):
        await service.create(
            owner,
            request.model_copy(update={"display_name": "Other"}),
            key="create-1",
            ceiling=Sensitivity.SENSITIVE,
        )
    page = await service.list(owner, text="alex", ceiling=Sensitivity.RESTRICTED)
    assert page.items == [person]
    assert (await service.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)).person == person
    with pytest.raises(NotFoundError):
        await service.get(
            owner.model_copy(update={"principal_id": "foreign"}),
            person.id,
            ceiling=Sensitivity.SENSITIVE,
        )
    with pytest.raises(NotFoundError):
        await service.get(owner, person.id, ceiling=Sensitivity.INTERNAL)
    update = UpdatePerson(
        session_id=session().id, expected_revision=1, display_name="Alex Rivera", pinned=True
    )
    changed = await service.update(
        owner, person.id, update, key="edit-1", ceiling=Sensitivity.SENSITIVE
    )
    assert changed.revision == 2 and changed.pinned
    assert changed.display_name == "Alex Rivera"
    assert (
        await service.update(owner, person.id, update, key="edit-1", ceiling=Sensitivity.SENSITIVE)
        == changed
    )
    with pytest.raises(ConflictError):
        await service.update(owner, person.id, update, key="edit-2", ceiling=Sensitivity.SENSITIVE)


async def test_history_is_cross_session_paged_and_evidence_requires_source_scope() -> None:
    from datetime import timedelta
    from uuid import uuid4

    from agent_core.domain.people import InteractionParticipant, PeopleInteraction, PeopleSource
    from agent_core.domain.people_views import PeopleSectionQuery
    from tests.contract.support import NOW

    clock, factory = await memory_uow_factory()
    service = PublicPeopleService(factory, clock)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Sam"),
        key="sam",
        ceiling=Sensitivity.SENSITIVE,
    )
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    source = PeopleSource(
        id=uuid4(),
        session_id=session().id,
        event_sequence=1,
        source_kind="owner",
        evidence_at=NOW,
        source_revision="owner@1",
        **common,
    )
    rows = [
        PeopleInteraction(
            id=uuid4(),
            channel="chat",
            interaction_kind="meeting",
            attribution="owner_reported",
            direction="reported",
            summary=f"Meeting {i}",
            occurred_at=NOW - timedelta(days=i),
            support_ids=[source.id],
            participants=[InteractionParticipant(person_id=person.id, role="participant")],
            **common,
        )
        for i in range(3)
    ]
    async with factory() as uow:
        await uow.people.put(source, expected_revision=0)
        for row in rows:
            await uow.people.put(row, expected_revision=0)
    query = PeopleSectionQuery(section="history", limit=2)
    first = await service.section(owner, person.id, query, ceiling=Sensitivity.SENSITIVE)
    assert first.items == rows[:2] and first.next_cursor
    second = await service.section(
        owner,
        person.id,
        query.model_copy(update={"cursor": first.next_cursor}),
        ceiling=Sensitivity.SENSITIVE,
    )
    assert second.items == rows[2:] and second.next_cursor is None
    with pytest.raises(AuthorizationError):
        await service.evidence(owner, person.id, source.id, ceiling=Sensitivity.SENSITIVE)
    scoped = owner.model_copy(update={"scopes": {*owner.scopes, "session.read"}})
    assert (
        await service.evidence(scoped, person.id, source.id, ceiling=Sensitivity.SENSITIVE)
    ).event_sequence == 1
    with pytest.raises(NotFoundError):
        await service.evidence(scoped, person.id, uuid4(), ceiling=Sensitivity.SENSITIVE)


async def test_affirmation_preserves_the_directed_relationship_projection() -> None:
    from uuid import uuid4

    from agent_core.domain.memory import BeliefType, Portability
    from agent_core.domain.people import RelationshipAssertion
    from agent_core.domain.people_views import PeopleCorrectionRequest
    from agent_core.memory.formation import GovernedMemoryService
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW, ids

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(
        factory, clock, memory_for=lambda p: GovernedMemoryService(factory, clock, ids(), p)
    )
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="maya",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory(statement="Maya is my sister").model_copy(
        update={"belief_type": BeliefType.RELATIONSHIP, "portability": Portability.CONTEXTUAL}
    )
    edge = RelationshipAssertion(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
        subject=PeopleEndpoint(kind="person", id=person.id),
        object=PeopleEndpoint(kind="owner"),
        predicate="sibling",
        belief_id=belief.id,
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(edge, expected_revision=0)
    result = await service.correct(
        owner,
        person.id,
        PeopleCorrectionRequest(
            session_id=session().id,
            belief_id=belief.id,
            expected_revision=1,
            expected_position=belief.store_position,
            operation="affirm",
        ),
        key="affirm",
        ceiling=Sensitivity.SENSITIVE,
    )
    profile = await service.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)
    assert len(profile.relationships) == 1
    assert (
        profile.relationships[0].subject == edge.subject
        and profile.relationships[0].object == edge.object
    )
    assert not profile.relationships[0].unresolved
    assert result.belief is not None
    assert profile.relationships[0].belief_id == result.belief.id


async def test_public_correction_requires_link_revision_and_retries_exactly() -> None:
    from uuid import uuid4

    from agent_core.domain.people import PersonMemoryLink
    from agent_core.domain.people_views import PeopleCorrectionRequest
    from agent_core.memory.formation import GovernedMemoryService
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW, ids

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(
        factory, clock, memory_for=lambda p: GovernedMemoryService(factory, clock, ids(), p)
    )
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Alex"),
        key="create",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory(statement="Alex lives in Paris")
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                person_id=person.id,
                belief_id=belief.id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
    request = PeopleCorrectionRequest(
        session_id=session().id,
        belief_id=belief.id,
        expected_revision=person.revision,
        expected_position=belief.store_position,
        operation="changed",
        statement="Alex lives in Berlin",
    )
    changed = await service.correct(
        owner, person.id, request, key="change", ceiling=Sensitivity.SENSITIVE
    )
    assert changed.belief is not None and changed.belief.statement == "Alex lives in Berlin"
    assert changed.person_revision == 2
    assert (
        await service.correct(
            owner, person.id, request, key="change", ceiling=Sensitivity.SENSITIVE
        )
        == changed
    )
    with pytest.raises(ConflictError):
        await service.correct(
            owner, person.id, request, key="another-change", ceiling=Sensitivity.SENSITIVE
        )


async def test_person_facts_expose_only_their_current_correction_revisions() -> None:
    from uuid import uuid4

    from agent_core.domain.people import PersonMemoryLink
    from tests.contract.memory_fixtures import memory

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(factory, clock)
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="maya",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory()
    async with factory() as uow:
        stored = await uow.memories.upsert_belief(belief)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=clock.now(),
                updated_at=clock.now(),
                person_id=person.id,
                belief_id=stored.id,
            ),
            expected_revision=0,
        )
    profile = await service.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)
    assert getattr(profile, "fact_revisions", {}) == {stored.id: stored.store_position}


async def test_people_history_uses_recorded_belief_and_effective_time() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await people_history_temporal_contract(factory, clock, owner)


async def people_history_temporal_contract(
    factory: UnitOfWorkFactory, clock: FixedClock, owner: Principal
) -> None:
    from datetime import timedelta
    from uuid import uuid4

    from agent_core.domain.memory import MemoryStatus
    from agent_core.domain.people import PersonMemoryLink
    from agent_core.domain.people_views import PeopleSectionQuery
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW

    service = PublicPeopleService(factory, clock)
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="history",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory().model_copy(update={"statement": "Maya lives in Paris"})
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                person_id=person.id,
                belief_id=belief.id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
    clock.advance(timedelta(days=2))
    async with factory() as uow:
        await uow.memories.reinforce(
            belief.model_copy(
                update={
                    "status": MemoryStatus.SUPERSEDED,
                    "valid_to": clock.now(),
                    "updated_at": clock.now(),
                    "store_position": 2,
                }
            )
        )
    historical = await service.section(
        owner,
        person.id,
        PeopleSectionQuery(section="facts", as_of=NOW),
        ceiling=Sensitivity.SENSITIVE,
    )
    assert len(historical.items) == 1 and isinstance(historical.items[0], MemoryView)
    assert historical.items[0].statement == "Maya lives in Paris"
    earlier = await service.section(
        owner,
        person.id,
        PeopleSectionQuery(section="facts", known_at=NOW),
        ceiling=Sensitivity.SENSITIVE,
    )
    assert len(earlier.items) == 1 and isinstance(earlier.items[0], MemoryView)
    assert earlier.items[0].statement == "Maya lives in Paris"
    assert earlier.items[0].status == MemoryStatus.ACTIVE
    assert not (await service.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)).facts

    async with factory() as uow:
        live = await uow.memories.get(belief.id, owner)
        await uow.memories.reinforce(
            live.model_copy(
                update={
                    "statement": "A later correction recorded after the requested cutoff",
                    "updated_at": clock.now() + timedelta(seconds=1),
                }
            )
        )
    repair = await service.section(
        owner,
        person.id,
        PeopleSectionQuery(section="identity-evidence", known_at=NOW),
        ceiling=Sensitivity.SENSITIVE,
    )
    labels = [
        getattr(item, "label", None)
        for item in repair.items
        if getattr(item, "belief_id", None) == belief.id
    ]
    assert labels == ["Maya lives in Paris"]


async def test_profile_excludes_expired_facts_before_sweep() -> None:
    from datetime import timedelta
    from uuid import uuid4

    from agent_core.domain.people import PersonMemoryLink
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(factory, clock)
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="expired",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory().model_copy(update={"expires_at": NOW - timedelta(seconds=1)})
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                person_id=person.id,
                belief_id=belief.id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
    assert not (await service.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)).facts


async def test_owner_alias_assignment_is_revisioned_and_resolves_until_ended() -> None:
    from datetime import timedelta

    from agent_core.domain.people_views import AddPersonAlias, EndPersonAlias
    from agent_core.memory.people import resolve_identity

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    service = PublicPeopleService(factory, clock)
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="alias-person",
        ceiling=Sensitivity.SENSITIVE,
    )
    changed = await service.update(
        owner,
        person.id,
        UpdatePerson(
            session_id=session().id,
            expected_revision=1,
            alias=AddPersonAlias(
                operation="add", identifier_kind="email", value="maya@example.test"
            ),
        ),
        key="add-alias",
        ceiling=Sensitivity.SENSITIVE,
    )
    profile = await service.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)
    alias = next((item for item in profile.aliases if item.value == "maya@example.test"), None)
    assert alias is not None and alias.verification == "owner_confirmed"
    assert [
        item.id
        for item in (
            await service.list(owner, text="maya@example.test", ceiling=Sensitivity.SENSITIVE)
        ).items
    ] == [person.id]
    async with factory() as uow:
        resolved = await resolve_identity(
            uow.people,
            owner,
            kind="email",
            namespace="owner",
            value=alias.value,
            context="owner",
            at=clock.now(),
            ceiling=Sensitivity.SENSITIVE,
        )
        assert resolved.person_ids == [person.id] and resolved.status == "matched"
    clock.advance(timedelta(days=1))
    await service.update(
        owner,
        person.id,
        UpdatePerson(
            session_id=session().id,
            expected_revision=changed.revision,
            alias=EndPersonAlias(
                operation="end", identifier_id=alias.id, expected_revision=1, valid_to=clock.now()
            ),
        ),
        key="end-alias",
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        resolved = await resolve_identity(
            uow.people,
            owner,
            kind="email",
            namespace="owner",
            value=alias.value,
            context="owner",
            at=clock.now(),
            ceiling=Sensitivity.SENSITIVE,
        )
        assert resolved.status == "unresolved"
    old = await service.list(
        owner, text=alias.value, ceiling=Sensitivity.SENSITIVE, as_of=alias.valid_from
    )
    current = await service.list(owner, text=alias.value, ceiling=Sensitivity.SENSITIVE)
    assert [item.id for item in old.items] == [person.id]
    assert current.items == []


@pytest.mark.parametrize("operation", ["correct", "changed"])
@pytest.mark.parametrize("dated", [False, True])
async def test_correction_distinguishes_incorrect_fact_from_changed_relationship(
    operation: Literal["correct", "changed"], dated: bool
) -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await people_correction_temporal_contract(factory, clock, owner, operation, dated)


async def people_correction_temporal_contract(
    factory: UnitOfWorkFactory,
    clock: FixedClock,
    owner: Principal,
    operation: Literal["correct", "changed"],
    dated: bool,
) -> None:
    from datetime import timedelta
    from uuid import uuid4

    from agent_core.domain.memory import BeliefType, Portability
    from agent_core.domain.people import RelationshipAssertion
    from agent_core.domain.people_views import PeopleCorrectionRequest, PeopleSectionQuery
    from agent_core.memory.formation import GovernedMemoryService
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW, ids

    service = PublicPeopleService(
        factory, clock, memory_for=lambda p: GovernedMemoryService(factory, clock, ids(), p)
    )
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="maya",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory(statement="Maya is my colleague").model_copy(
        update={
            "belief_type": BeliefType.RELATIONSHIP,
            "portability": Portability.CONTEXTUAL,
        }
    )
    edge = RelationshipAssertion(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
        subject=PeopleEndpoint(kind="person", id=person.id),
        object=PeopleEndpoint(kind="owner"),
        predicate="colleague",
        belief_id=belief.id,
        valid_from=NOW,
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(edge, expected_revision=0)
    clock.advance(timedelta(days=5))
    result = await service.correct(
        owner,
        person.id,
        PeopleCorrectionRequest(
            session_id=session().id,
            belief_id=belief.id,
            expected_revision=1,
            expected_position=belief.store_position,
            operation=operation,
            effective_at=NOW + timedelta(days=3) if dated and operation == "changed" else None,
            statement="Maya is my former colleague"
            if operation == "changed"
            else "Maya is my neighbor",
        ),
        key="correct",
        ceiling=Sensitivity.SENSITIVE,
    )
    world = NOW + timedelta(days=1)
    known_before = await service.section(
        owner,
        person.id,
        PeopleSectionQuery(section="relationships", as_of=world, known_at=world),
        ceiling=Sensitivity.SENSITIVE,
    )
    assert len(known_before.items) == 1
    known_now = await service.section(
        owner,
        person.id,
        PeopleSectionQuery(section="relationships", as_of=world),
        ceiling=Sensitivity.SENSITIVE,
    )
    assert len(known_now.items) == (1 if operation == "changed" else 0)
    async with factory() as uow:
        old = await uow.memories.get(belief.id, owner)
        from agent_core.domain.memory import MemoryRecord

        MemoryRecord.model_validate(old.model_dump())
    expected = NOW + timedelta(days=3) if dated else clock.now()
    assert old.valid_to == (expected if operation == "changed" else NOW)
    assert result.belief is not None
    assert result.belief.valid_from == (expected if operation == "changed" else NOW)


@pytest.mark.parametrize("kind", ["relationship", "commitment"])
@pytest.mark.parametrize(
    "operation,dated", [("correct", False), ("changed", False), ("changed", True)]
)
async def test_structured_owner_change_replaces_projection_and_preserves_known_history(
    kind: str,
    operation: Literal["correct", "changed"],
    dated: bool,
) -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await people_projection_temporal_contract(factory, clock, owner, kind, operation, dated)


async def people_projection_temporal_contract(
    factory: UnitOfWorkFactory,
    clock: FixedClock,
    owner: Principal,
    kind: str,
    operation: Literal["correct", "changed"] = "changed",
    dated: bool = True,
) -> None:
    from datetime import timedelta
    from uuid import uuid4

    from agent_core.domain.memory import BeliefType, Portability
    from agent_core.domain.people import PeopleCommitment, PersonMemoryLink, RelationshipAssertion
    from agent_core.domain.people_views import PeopleCorrectionRequest, PeopleSectionQuery
    from agent_core.memory.formation import GovernedMemoryService
    from tests.contract.memory_fixtures import memory
    from tests.contract.support import NOW, ids

    service = PublicPeopleService(
        factory, clock, memory_for=lambda p: GovernedMemoryService(factory, clock, ids(), p)
    )
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="maya",
        ceiling=Sensitivity.SENSITIVE,
    )
    belief = memory(
        statement="Maya is my colleague" if kind == "relationship" else "Maya promised a report"
    ).model_copy(
        update={"belief_type": BeliefType.RELATIONSHIP, "portability": Portability.CONTEXTUAL}
    )
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    subject = PeopleEndpoint(kind="person", id=person.id)
    target = PeopleEndpoint(kind="owner")
    edge: RelationshipAssertion | PeopleCommitment
    if kind == "relationship":
        edge = RelationshipAssertion(
            id=uuid4(),
            **common,
            subject=subject,
            object=target,
            predicate="colleague",
            belief_id=belief.id,
            valid_from=NOW,
            support_ids=person.support_ids,
        )
        fields: dict[str, object] = {
            "predicate": "friend",
            "subject": subject.model_dump(mode="json"),
            "object": target.model_dump(mode="json"),
            "precision": "month",
            "source_timezone": "America/Los_Angeles",
        }
    else:
        edge = PeopleCommitment(
            id=uuid4(),
            **common,
            debtor=subject,
            beneficiary=target,
            state="open",
            description="Maya promised a report",
            belief_id=belief.id,
            support_ids=person.support_ids,
            state_source_id=person.support_ids[0],
        )
        fields = {
            "state": "completed",
            "description": "Maya delivered the report",
            "debtor": subject.model_dump(mode="json"),
            "beneficiary": target.model_dump(mode="json"),
            "due_at": "2026-08-01T00:00:00-07:00",
            "due_precision": "month",
            "source_timezone": "America/Los_Angeles",
        }
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
        await uow.people.put(edge, expected_revision=0)
        await uow.people.put(
            PersonMemoryLink(
                id=uuid4(),
                **common,
                person_id=person.id,
                belief_id=belief.id,
                support_ids=person.support_ids,
            ),
            expected_revision=0,
        )
    clock.advance(timedelta(days=5))
    request = PeopleCorrectionRequest.model_validate(
        {
            "session_id": session().id,
            "belief_id": belief.id,
            "expected_revision": person.revision,
            "expected_position": belief.store_position,
            "operation": operation,
            "effective_at": NOW + timedelta(days=3) if dated else None,
            "statement": "Maya is my friend"
            if kind == "relationship"
            else "Maya delivered the report",
            "projection": {"kind": kind, "id": edge.id, "expected_revision": 1, **fields},
        }
    )
    assert request.projection is not None
    stale = request.model_copy(
        update={"projection": request.projection.model_copy(update={"expected_revision": 2})}
    )
    with pytest.raises(ConflictError, match="projection revision"):
        await service.correct(
            owner, person.id, stale, key="stale-projection", ceiling=Sensitivity.SENSITIVE
        )
    missing = request.model_copy(
        update={"projection": request.projection.model_copy(update={"id": uuid4()})}
    )
    with pytest.raises(NotFoundError, match="projection not found"):
        await service.correct(
            owner, person.id, missing, key="missing-projection", ceiling=Sensitivity.SENSITIVE
        )
    result = await service.correct(
        owner, person.id, request, key="projection", ceiling=Sensitivity.SENSITIVE
    )
    assert (
        await service.correct(
            owner, person.id, request, key="projection", ceiling=Sensitivity.SENSITIVE
        )
        == result
    )
    profile = await service.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)
    current = profile.relationships if kind == "relationship" else profile.commitments
    assert len(current) == 1 and current[0].id != edge.id
    assert result.belief is not None and current[0].belief_id == result.belief.id
    assert current[0].support_ids != edge.support_ids
    if kind == "commitment":
        assert current[0].model_dump()["due_precision"] == "month"
        assert current[0].model_dump()["source_timezone"] == "America/Los_Angeles"
        assert profile.model_dump(mode="json")["commitments"][0]["due_precision"] == "month"
    if isinstance(current[0], RelationshipAssertion):
        start = NOW if operation == "correct" else NOW + timedelta(days=3) if dated else clock.now()
        assert current[0].predicate == "friend" and current[0].valid_from == start
        assert current[0].precision == "month"
        assert current[0].model_dump()["source_timezone"] == "America/Los_Angeles"
    else:
        assert (
            current[0].state == "completed" and current[0].state_source_id in current[0].support_ids
        )
    async with factory() as uow:
        past = await uow.people.get(
            owner, edge.id, ceiling=Sensitivity.SENSITIVE, known_at=NOW + timedelta(days=1)
        )
        assert past == edge
    world_before = NOW + timedelta(days=1)
    recorded_before = await service.section(
        owner,
        person.id,
        PeopleSectionQuery(section="facts", as_of=world_before, known_at=world_before),
        ceiling=Sensitivity.SENSITIVE,
    )
    assert [item.id for item in recorded_before.items] == [belief.id]
    corrected_past = await service.section(
        owner,
        person.id,
        PeopleSectionQuery(section="facts", as_of=world_before),
        ceiling=Sensitivity.SENSITIVE,
    )
    expected_past = result.belief.id if operation == "correct" else belief.id
    assert [item.id for item in corrected_past.items] == [expected_past]
    recorded_after = await service.section(
        owner,
        person.id,
        PeopleSectionQuery(section="facts", as_of=clock.now()),
        ceiling=Sensitivity.SENSITIVE,
    )
    assert [item.id for item in recorded_after.items] == [result.belief.id]


async def test_email_evidence_resolves_only_the_matching_owned_retained_thread() -> None:
    import hashlib
    from uuid import uuid4

    from agent_core.domain.email import EmailMessage, EmailRecord, EmailThread
    from agent_core.domain.people import PeopleSource
    from tests.contract.support import NOW

    clock, factory = await memory_uow_factory()
    service = PublicPeopleService(factory, clock)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write", "email.read"}})
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="maya",
        ceiling=Sensitivity.SENSITIVE,
    )
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    source = PeopleSource(
        id=uuid4(),
        **common,
        session_id=session().id,
        event_sequence=1,
        source_kind="email",
        evidence_at=NOW,
        source_revision="email@1",
        account_id="personal",
        thread_id="gmail-thread",
        message_id="gmail-message",
    )
    thread = EmailThread(
        id=uuid4(),
        account_id="personal",
        provider_thread_id="gmail-thread",
        subject="Synthetic source",
        messages=[
            EmailMessage(
                id="gmail-message",
                sender="maya@example.test",
                body="Synthetic message",
                sent_at=NOW,
            )
        ],
        updated_at=NOW,
        last_accessed_at=NOW,
    )
    source_key = hashlib.sha256(b"personal:gmail-thread").hexdigest()
    async with factory() as uow:
        await uow.people.put(source, expected_revision=0)
        await uow.people.put(
            person.model_copy(update={"revision": 2, "support_ids": [source.id]}),
            expected_revision=1,
        )
        await uow.email.put(
            EmailRecord(
                **common,
                kind="thread_source",
                key=source_key,
                revision=1,
                payload={"thread_id": str(thread.id)},
            ),
            expected_revision=0,
        )
        await uow.email.put(
            EmailRecord(
                **common,
                kind="thread",
                key=str(thread.id),
                revision=1,
                payload=thread.model_dump(mode="json"),
            ),
            expected_revision=0,
        )
    evidence = await service.evidence(owner, person.id, source.id, ceiling=Sensitivity.SENSITIVE)
    assert evidence.model_dump()["email_thread_id"] == thread.id
    async with factory() as uow:
        row = await uow.email.get(owner, "thread", str(thread.id))
        assert row is not None
        await uow.email.put(
            row.model_copy(
                update={
                    "revision": 2,
                    "payload": thread.model_copy(update={"account_id": "work"}).model_dump(
                        mode="json"
                    ),
                }
            ),
            expected_revision=1,
        )
    unavailable = await service.evidence(owner, person.id, source.id, ceiling=Sensitivity.SENSITIVE)
    assert unavailable.model_dump()["email_thread_id"] is None


async def test_owner_identity_evidence_reads_the_original_owner_assertion() -> None:
    clock, factory = await memory_uow_factory()
    service = PublicPeopleService(factory, clock)
    owner = principal().model_copy(
        update={"scopes": {"people.read", "people.write", "session.read"}}
    )
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Maya"),
        key="maya",
        ceiling=Sensitivity.SENSITIVE,
    )
    evidence = await service.evidence(
        owner, person.id, person.support_ids[0], ceiling=Sensitivity.SENSITIVE
    )
    assert evidence.model_dump()["owner_assertion"] == "Owner created person: Maya"


async def test_profile_lists_each_alias_once() -> None:
    """Per-message copies of one alias appear once and cannot crowd out history."""
    from uuid import uuid4

    from agent_core.domain.people import (
        InteractionParticipant,
        PeopleInteraction,
        Person,
        PersonIdentifier,
    )
    from tests.contract.support import NOW

    clock, factory = await memory_uow_factory()
    service = PublicPeopleService(factory, clock)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Frequent Correspondent", **common)
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        for _ in range(1200):
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="email",
                    namespace="owner",
                    value="frequent@example.test",
                    context="owner",
                    verification="channel_observed",
                    valid_from=NOW,
                    **common,
                ),
                expected_revision=0,
            )
        await uow.people.put(
            PeopleInteraction(
                id=uuid4(),
                channel="email",
                interaction_kind="exchange",
                attribution="observed",
                direction="outgoing",
                summary="Sent email",
                occurred_at=NOW,
                participants=[InteractionParticipant(person_id=person.id, role="recipient")],
                **common,
            ),
            expected_revision=0,
        )
    profile = await service.get(owner, person.id, ceiling=Sensitivity.RESTRICTED)
    assert [alias.value for alias in profile.aliases] == ["frequent@example.test"]
    assert len(profile.history) == 1


async def test_ending_an_alias_ends_every_observed_copy_of_that_assignment() -> None:
    """The profile shows one alias per assignment, so ending it ends every copy (ADR-0118)."""
    from datetime import timedelta
    from uuid import uuid4

    from agent_core.domain.people import Person, PersonIdentifier
    from agent_core.domain.people_views import EndPersonAlias
    from agent_core.memory.people import resolve_identity
    from tests.contract.support import NOW

    clock, factory = await memory_uow_factory()
    service = PublicPeopleService(factory, clock)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Alex Rivera", **common)
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        # Correspondence writes one observed copy of the address per message.
        for value, days in (
            ("alex@example.test", 30),
            ("alex@example.test", 20),
            ("alex@example.test", 10),
            ("alex@home.test", 30),
        ):
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="email",
                    namespace="owner",
                    value=value,
                    context="owner",
                    verification="channel_observed",
                    valid_from=NOW - timedelta(days=days),
                    **common,
                ),
                expected_revision=0,
            )
    profile = await service.get(owner, person.id, ceiling=Sensitivity.SENSITIVE)
    [shown] = [alias for alias in profile.aliases if alias.value == "alex@example.test"]
    await service.update(
        owner,
        person.id,
        UpdatePerson(
            session_id=session().id,
            expected_revision=person.revision,
            alias=EndPersonAlias(
                operation="end",
                identifier_id=shown.id,
                expected_revision=shown.revision,
                valid_to=clock.now(),
            ),
        ),
        key="end-work-address",
        ceiling=Sensitivity.SENSITIVE,
    )
    clock.advance(timedelta(seconds=1))
    async with factory() as uow:
        for value, status in (("alex@example.test", "unresolved"), ("alex@home.test", "matched")):
            resolved = await resolve_identity(
                uow.people,
                owner,
                kind="email",
                namespace="owner",
                value=value,
                context="owner",
                at=clock.now(),
                ceiling=Sensitivity.SENSITIVE,
            )
            assert resolved.status == status, value


async def test_owner_created_person_gets_an_owner_confirmed_name_alias() -> None:
    """A person the owner adds is found again when the owner names them in chat (ADR-0118)."""
    from agent_core.domain.people import PeopleQuery, PersonIdentifier
    from agent_core.memory.people import resolve_identity

    clock, factory = await memory_uow_factory()
    service = PublicPeopleService(factory, clock)
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    person = await service.create(
        owner,
        CreatePerson(session_id=session().id, display_name="Kyrri"),
        key="create-kyrri",
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        aliases = await uow.people.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                kinds=["identifier"],
                person_id=person.id,
                sensitivity_ceiling=Sensitivity.RESTRICTED,
            )
        )
        resolved = await resolve_identity(
            uow.people,
            owner,
            kind="name",
            namespace="owner",
            value="kyrri",
            context="owner",
            at=clock.now(),
            ceiling=Sensitivity.RESTRICTED,
        )
    assert [
        (a.identifier_kind, a.value, a.verification)
        for a in aliases
        if isinstance(a, PersonIdentifier)
    ] == [("name", "Kyrri", "owner_confirmed")]
    assert (resolved.status, resolved.person_ids) == ("matched", [person.id])
