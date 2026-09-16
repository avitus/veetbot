"""Identity repairs require explicit revisions and compatible undo."""

from typing import Any, cast
from uuid import uuid4

import pytest

from agent_core.application.people_identity import PeopleIdentityService
from agent_core.domain.errors import ConflictError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleRecord, Person, PersonIdentifier
from tests.contract.support import NOW, ids, memory_uow_factory, principal


async def test_merge_is_previewed_reversible_and_rejects_stale_assignments() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    first = Person(id=uuid4(), display_name="Alex", **common)
    second = Person(id=uuid4(), display_name="Alex Rivera", **common)
    alias = PersonIdentifier(
        id=uuid4(),
        person_id=first.id,
        identifier_kind="name",
        namespace="owner",
        value="Al",
        verification="owner_confirmed",
        valid_from=NOW,
        **common,
    )
    async with factory() as uow:
        for row in (first, second, alias):
            await uow.people.put(row, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())
    preview = await service.preview_merge(
        owner,
        first.id,
        second.id,
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        assert await uow.people.get(owner, first.id, ceiling=Sensitivity.SENSITIVE) == first
    complete = await service.apply(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    assert complete.state == "completed"
    assert await service.apply(owner, preview.id, ceiling=Sensitivity.SENSITIVE) == complete
    async with factory() as uow:
        merged = await uow.people.get(owner, first.id, ceiling=Sensitivity.SENSITIVE)
        reassigned = await uow.people.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE)
    assert isinstance(merged, Person) and merged.merged_into == second.id
    assert isinstance(reassigned, PersonIdentifier) and reassigned.person_id == second.id
    undo = await service.preview_undo(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    await service.apply(owner, undo.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        restored = await uow.people.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE)
    assert isinstance(restored, PersonIdentifier) and restored.person_id == first.id
    with pytest.raises(ConflictError):
        await service.preview_merge(
            owner,
            first.id,
            second.id,
            expected_revisions={first.id: 1, second.id: 1},
            ceiling=Sensitivity.SENSITIVE,
        )


async def test_undo_preserves_both_original_group_participants() -> None:
    from agent_core.domain.people import InteractionParticipant, PeopleInteraction

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    people = [Person(id=uuid4(), display_name=name, **common) for name in ("Alex", "Al")]
    interaction = PeopleInteraction(
        id=uuid4(),
        channel="chat",
        interaction_kind="meeting",
        attribution="owner_reported",
        direction="reported",
        summary="We met",
        participants=[InteractionParticipant(person_id=p.id, role="participant") for p in people],
        **common,
    )
    async with factory() as uow:
        for person in people:
            await uow.people.put(person, expected_revision=0)
        await uow.people.put(interaction, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())
    preview = await service.preview_merge(
        owner,
        people[0].id,
        people[1].id,
        expected_revisions={p.id: 1 for p in people},
        ceiling=Sensitivity.SENSITIVE,
    )
    await service.apply(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    undo = await service.preview_undo(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    await service.apply(owner, undo.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        restored = await uow.people.get(owner, interaction.id, ceiling=Sensitivity.SENSITIVE)
    assert isinstance(restored, PeopleInteraction)
    assert restored.participants == interaction.participants


async def test_split_moves_selected_evidence_and_can_be_undone() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    first, second = [Person(id=uuid4(), display_name="Alex", **common) for _ in range(2)]
    aliases = [
        PersonIdentifier(
            id=uuid4(),
            person_id=first.id,
            identifier_kind="email",
            namespace="email",
            value=f"alex{i}@example.test",
            verification="owner_confirmed",
            valid_from=NOW,
            **common,
        )
        for i in range(2)
    ]
    async with factory() as uow:
        for row in cast(tuple[PeopleRecord, ...], (first, second, *aliases)):
            await uow.people.put(row, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())
    preview = await service.preview_split(
        owner,
        first.id,
        second.id,
        selected_ids=[aliases[0].id],
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )
    await service.apply(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        moved = await uow.people.get(owner, aliases[0].id, ceiling=Sensitivity.SENSITIVE)
        kept = await uow.people.get(owner, aliases[1].id, ceiling=Sensitivity.SENSITIVE)
        assert isinstance(moved, PersonIdentifier) and moved.person_id == second.id
        assert isinstance(kept, PersonIdentifier) and kept.person_id == first.id
        retained_person = await uow.people.get(owner, first.id, ceiling=Sensitivity.SENSITIVE)
        assert isinstance(retained_person, Person) and retained_person.state != "merged"
    undo = await service.preview_undo(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    await service.apply(owner, undo.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        restored = await uow.people.get(owner, aliases[0].id, ceiling=Sensitivity.SENSITIVE)
        assert isinstance(restored, PersonIdentifier) and restored.person_id == first.id


async def test_operation_visibility_tracks_assignment_privacy_and_erasure() -> None:
    from datetime import timedelta

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
        "sensitivity": Sensitivity.INTERNAL,
    }
    first, second = [Person(id=uuid4(), display_name=n, **common) for n in ("A", "B")]
    alias = PersonIdentifier(
        id=uuid4(),
        person_id=first.id,
        identifier_kind="name",
        namespace="owner",
        value="Al",
        verification="owner_confirmed",
        valid_from=NOW,
        **common,
    )
    async with factory() as uow:
        for row in (first, second, alias):
            await uow.people.put(row, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())
    preview = await service.preview_merge(
        owner,
        first.id,
        second.id,
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        await uow.people.put(
            alias.model_copy(
                update={
                    "sensitivity": Sensitivity.RESTRICTED,
                    "revision": 2,
                    "updated_at": NOW + timedelta(seconds=1),
                }
            ),
            expected_revision=1,
        )
        assert await uow.people.get(owner, preview.id, ceiling=Sensitivity.SENSITIVE) is None
        await uow.people.erase(owner, [alias.id])
        assert await uow.people.get(owner, preview.id, ceiling=Sensitivity.RESTRICTED) is None


async def test_split_marks_mixed_source_claim_unresolved_and_restores_on_undo() -> None:
    from agent_core.domain.people import PeopleSource, PersonMemoryLink

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    first, second = [Person(id=uuid4(), display_name="Alex", **common) for _ in range(2)]
    sources = [
        PeopleSource(
            id=uuid4(),
            session_id=uuid4(),
            event_sequence=1,
            source_kind="owner",
            source_revision="owner@1",
            evidence_at=NOW,
            **common,
        )
        for _ in range(2)
    ]
    aliases = [
        PersonIdentifier(
            id=uuid4(),
            person_id=first.id,
            identifier_kind="email",
            namespace="email",
            value=f"alex{i}@example.test",
            verification="owner_confirmed",
            valid_from=NOW,
            support_ids=[source.id],
            **common,
        )
        for i, source in enumerate(sources)
    ]
    claim = PersonMemoryLink(
        id=uuid4(),
        person_id=first.id,
        belief_id=uuid4(),
        support_ids=[source.id for source in sources],
        **common,
    )
    async with factory() as uow:
        for row in cast(tuple[PeopleRecord, ...], (first, second, *sources, *aliases, claim)):
            await uow.people.put(row, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())
    preview = await service.preview_split(
        owner,
        first.id,
        second.id,
        selected_ids=[aliases[0].id],
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )
    await service.apply(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        unresolved = await uow.people.get(owner, claim.id, ceiling=Sensitivity.SENSITIVE)
        assert unresolved is not None and getattr(unresolved, "unresolved", False)
    undo = await service.preview_undo(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    await service.apply(owner, undo.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        restored = await uow.people.get(owner, claim.id, ceiling=Sensitivity.SENSITIVE)
        assert restored is not None and not getattr(restored, "unresolved", False)


async def test_identity_request_key_binds_preview_payload_and_apply_revision() -> None:
    from agent_core.domain.people_views import PeopleIdentityRequest
    from tests.contract.support import session

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    first, second = [Person(id=uuid4(), display_name=n, **common) for n in ("A", "B")]
    async with factory() as uow:
        for row in (first, second):
            await uow.people.put(row, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())
    request = PeopleIdentityRequest(
        session_id=session().id,
        operation="merge",
        source_id=first.id,
        target_id=second.id,
        expected_revisions={first.id: 1, second.id: 1},
    )
    preview = await service.request(owner, request, key="preview", ceiling=Sensitivity.SENSITIVE)
    assert (
        await service.request(owner, request, key="preview", ceiling=Sensitivity.SENSITIVE)
        == preview
    )
    with pytest.raises(ConflictError):
        await service.request(
            owner,
            request.model_copy(update={"operation": "split"}),
            key="preview",
            ceiling=Sensitivity.SENSITIVE,
        )
    apply = PeopleIdentityRequest(
        session_id=session().id, operation="apply", operation_id=preview.id
    )
    completed = await service.request(owner, apply, key="apply", ceiling=Sensitivity.SENSITIVE)
    assert completed.state == "completed"
    assert (
        await service.request(owner, apply, key="apply", ceiling=Sensitivity.SENSITIVE) == completed
    )


async def test_split_rejects_new_unselected_evidence_after_preview() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    common: dict[str, Any] = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    first, second = [Person(id=uuid4(), display_name="Alex", **common) for _ in range(2)]
    alias = PersonIdentifier(
        id=uuid4(),
        person_id=first.id,
        identifier_kind="name",
        namespace="owner",
        value="Alex",
        context="Acme",
        verification="contextual",
        valid_from=NOW,
        **common,
    )
    async with factory() as uow:
        for row in (first, second, alias):
            await uow.people.put(row, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())
    preview = await service.preview_split(
        owner,
        first.id,
        second.id,
        selected_ids=[alias.id],
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        await uow.people.put(
            alias.model_copy(update={"id": uuid4(), "context": "choir"}), expected_revision=0
        )
    with pytest.raises(ConflictError, match="assignments changed"):
        await service.apply(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        assert await uow.people.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE) == alias
