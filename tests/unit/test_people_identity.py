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


def _common(owner: Any, **extra: Any) -> dict[str, Any]:
    return {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
        **extra,
    }


async def _pair_with_alias(
    factory: Any, owner: Any, *, alias_sensitivity: Sensitivity = Sensitivity.SENSITIVE
) -> tuple[Person, Person, PersonIdentifier]:
    first = Person(id=uuid4(), display_name="Alex", **_common(owner))
    second = Person(id=uuid4(), display_name="Alex Rivera", **_common(owner))
    alias = PersonIdentifier(
        id=uuid4(),
        person_id=first.id,
        identifier_kind="name",
        namespace="owner",
        value="Al",
        verification="owner_confirmed",
        valid_from=NOW,
        **_common(owner, sensitivity=alias_sensitivity),
    )
    async with factory() as uow:
        for row in (first, second, alias):
            await uow.people.put(row, expected_revision=0)
    return first, second, alias


async def test_merge_preview_expires_and_an_expired_preview_changes_nothing() -> None:
    """Preview tokens carry a short expiry; applying late is a conflict, not a merge."""

    from datetime import timedelta

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, alias = await _pair_with_alias(factory, owner)
    service = PeopleIdentityService(factory, clock, ids())
    revisions = {first.id: 1, second.id: 1}
    late = await service.preview_merge(
        owner, first.id, second.id, expected_revisions=revisions, ceiling=Sensitivity.SENSITIVE
    )
    clock.advance(timedelta(minutes=10))

    with pytest.raises(ConflictError, match="preview expired"):
        await service.apply(owner, late.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        assert await uow.people.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE) == alias
        assert await uow.people.get(owner, first.id, ceiling=Sensitivity.SENSITIVE) == first

    fresh = await service.preview_merge(
        owner, first.id, second.id, expected_revisions=revisions, ceiling=Sensitivity.SENSITIVE
    )
    clock.advance(timedelta(minutes=9, seconds=59))
    assert (await service.apply(owner, fresh.id, ceiling=Sensitivity.SENSITIVE)).state == (
        "completed"
    )


async def test_apply_refuses_a_preview_whose_identity_revision_moved() -> None:
    from datetime import timedelta

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, alias = await _pair_with_alias(factory, owner)
    service = PeopleIdentityService(factory, clock, ids())
    preview = await service.preview_merge(
        owner,
        first.id,
        second.id,
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )
    renamed = second.model_copy(
        update={
            "display_name": "Alexandra Rivera",
            "revision": 2,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    async with factory() as uow:
        await uow.people.put(renamed, expected_revision=1)

    with pytest.raises(ConflictError, match="identity revision changed"):
        await service.apply(owner, preview.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        assert await uow.people.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE) == alias


async def test_undo_after_a_later_conflicting_edit_requires_a_fresh_preview() -> None:
    """Inverse operations restore assignments only while revisions permit."""

    from datetime import timedelta

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, alias = await _pair_with_alias(factory, owner)
    service = PeopleIdentityService(factory, clock, ids())
    merge = await service.preview_merge(
        owner,
        first.id,
        second.id,
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )
    await service.apply(owner, merge.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        moved = await uow.people.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE)
        assert isinstance(moved, PersonIdentifier)
        await uow.people.put(
            moved.model_copy(
                update={
                    "value": "Ali",
                    "revision": moved.revision + 1,
                    "updated_at": moved.updated_at + timedelta(seconds=1),
                }
            ),
            expected_revision=moved.revision,
        )

    with pytest.raises(ConflictError, match="assignment changed since repair"):
        await service.preview_undo(owner, merge.id, ceiling=Sensitivity.SENSITIVE)

    async with factory() as uow:
        survivor = await uow.people.get(owner, second.id, ceiling=Sensitivity.SENSITIVE)
        assert isinstance(survivor, Person)
        await uow.people.put(
            survivor.model_copy(
                update={
                    "display_name": "Alex R.",
                    "revision": survivor.revision + 1,
                    "updated_at": survivor.updated_at + timedelta(seconds=1),
                }
            ),
            expected_revision=survivor.revision,
        )
    with pytest.raises(ConflictError, match="identity changed since repair"):
        await service.preview_undo(owner, merge.id, ceiling=Sensitivity.SENSITIVE)


async def test_only_a_completed_merge_or_split_can_be_undone() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, _alias = await _pair_with_alias(factory, owner)
    service = PeopleIdentityService(factory, clock, ids())
    merge = await service.preview_merge(
        owner,
        first.id,
        second.id,
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )

    with pytest.raises(ConflictError, match="cannot be undone"):
        await service.preview_undo(owner, merge.id, ceiling=Sensitivity.SENSITIVE)

    await service.apply(owner, merge.id, ceiling=Sensitivity.SENSITIVE)
    undo = await service.preview_undo(owner, merge.id, ceiling=Sensitivity.SENSITIVE)
    await service.apply(owner, undo.id, ceiling=Sensitivity.SENSITIVE)
    with pytest.raises(ConflictError, match="cannot be undone"):
        await service.preview_undo(owner, undo.id, ceiling=Sensitivity.SENSITIVE)


async def test_merge_preview_refuses_an_assignment_hidden_above_the_ceiling() -> None:
    """A hidden assignment must not silently survive a merge, nor be revealed by one."""

    from agent_core.domain.errors import NotFoundError

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, alias = await _pair_with_alias(
        factory, owner, alias_sensitivity=Sensitivity.RESTRICTED
    )
    service = PeopleIdentityService(factory, clock, ids())

    with pytest.raises(NotFoundError, match="identity not found"):
        await service.preview_merge(
            owner,
            first.id,
            second.id,
            expected_revisions={first.id: 1, second.id: 1},
            ceiling=Sensitivity.SENSITIVE,
        )
    async with factory() as uow:
        assert await uow.people.get(owner, alias.id, ceiling=Sensitivity.RESTRICTED) == alias
    preview = await service.preview_merge(
        owner,
        first.id,
        second.id,
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.RESTRICTED,
    )
    assert [change.entity_id for change in preview.assignments] == [alias.id]


async def test_merge_rewrites_relationship_and_commitment_endpoints_and_undo_restores_them() -> (
    None
):
    from agent_core.domain.people import (
        PeopleCommitment,
        PeopleEndpoint,
        PeopleSource,
        RelationshipAssertion,
    )

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, _alias = await _pair_with_alias(factory, owner)
    source = PeopleSource(
        id=uuid4(),
        session_id=uuid4(),
        event_sequence=1,
        source_kind="owner",
        source_revision="owner@1",
        evidence_at=NOW,
        **_common(owner),
    )
    source_id = source.id
    relationship = RelationshipAssertion(
        id=uuid4(),
        subject=PeopleEndpoint(kind="person", id=first.id),
        object=PeopleEndpoint(kind="owner"),
        predicate="colleague",
        belief_id=uuid4(),
        **_common(owner),
    )
    commitment = PeopleCommitment(
        id=uuid4(),
        debtor=PeopleEndpoint(kind="owner"),
        beneficiary=PeopleEndpoint(kind="person", id=first.id),
        description="Send the draft",
        state="open",
        belief_id=uuid4(),
        state_source_id=source_id,
        **_common(owner, support_ids=[source_id]),
    )
    async with factory() as uow:
        for row in cast(tuple[PeopleRecord, ...], (source, relationship, commitment)):
            await uow.people.put(row, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())
    merge = await service.preview_merge(
        owner,
        first.id,
        second.id,
        expected_revisions={first.id: 1, second.id: 1},
        ceiling=Sensitivity.SENSITIVE,
    )
    await service.apply(owner, merge.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        merged_relationship = await uow.people.get(
            owner, relationship.id, ceiling=Sensitivity.SENSITIVE
        )
        merged_commitment = await uow.people.get(
            owner, commitment.id, ceiling=Sensitivity.SENSITIVE
        )
    assert isinstance(merged_relationship, RelationshipAssertion)
    assert merged_relationship.subject == PeopleEndpoint(kind="person", id=second.id)
    assert merged_relationship.object == relationship.object
    assert merged_relationship.belief_id == relationship.belief_id
    assert isinstance(merged_commitment, PeopleCommitment)
    assert merged_commitment.beneficiary == PeopleEndpoint(kind="person", id=second.id)
    assert merged_commitment.debtor == commitment.debtor

    undo = await service.preview_undo(owner, merge.id, ceiling=Sensitivity.SENSITIVE)
    await service.apply(owner, undo.id, ceiling=Sensitivity.SENSITIVE)
    async with factory() as uow:
        restored_relationship = await uow.people.get(
            owner, relationship.id, ceiling=Sensitivity.SENSITIVE
        )
        restored_commitment = await uow.people.get(
            owner, commitment.id, ceiling=Sensitivity.SENSITIVE
        )
    assert isinstance(restored_relationship, RelationshipAssertion)
    assert restored_relationship.subject == relationship.subject
    assert isinstance(restored_commitment, PeopleCommitment)
    assert restored_commitment.beneficiary == commitment.beneficiary


async def test_merge_that_would_relate_a_person_to_itself_is_refused_before_preview() -> None:
    from agent_core.domain.people import PeopleEndpoint, RelationshipAssertion

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, _alias = await _pair_with_alias(factory, owner)
    between = RelationshipAssertion(
        id=uuid4(),
        subject=PeopleEndpoint(kind="person", id=first.id),
        object=PeopleEndpoint(kind="person", id=second.id),
        predicate="sibling",
        belief_id=uuid4(),
        **_common(owner),
    )
    async with factory() as uow:
        await uow.people.put(between, expected_revision=0)
    service = PeopleIdentityService(factory, clock, ids())

    with pytest.raises(ConflictError, match="resolving a dependent relationship"):
        await service.preview_merge(
            owner,
            first.id,
            second.id,
            expected_revisions={first.id: 1, second.id: 1},
            ceiling=Sensitivity.SENSITIVE,
        )
    async with factory() as uow:
        assert await uow.people.get(owner, between.id, ceiling=Sensitivity.SENSITIVE) == between


@pytest.mark.parametrize(
    ("update", "key", "error", "message"),
    [
        ({}, "", "validation", "idempotency key is invalid"),
        ({}, "k" * 201, "validation", "idempotency key is invalid"),
        ({"selected_ids": ["alias"]}, "merge-subset", "validation", "merge cannot select"),
        ({"target_id": None}, "no-target", "validation", "requires source and target"),
        ({"operation": "apply"}, "apply-extra", "validation", "only its preview identifier"),
        ({"source_id": "self"}, "self-merge", "conflict", "two distinct current identities"),
    ],
)
async def test_identity_request_rejects_malformed_operations_without_a_receipt(
    update: dict[str, Any], key: str, error: str, message: str
) -> None:
    from agent_core.domain.errors import ToolValidationError
    from agent_core.domain.people_views import PeopleIdentityRequest
    from tests.contract.support import session

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, alias = await _pair_with_alias(factory, owner)
    values: dict[str, Any] = {
        "session_id": session().id,
        "operation": "merge",
        "source_id": first.id,
        "target_id": second.id,
        "expected_revisions": {first.id: 1, second.id: 1},
    }
    for field, value in update.items():
        values[field] = (
            [alias.id] if value == ["alias"] else second.id if value == "self" else value
        )
    service = PeopleIdentityService(factory, clock, ids())
    expected = ToolValidationError if error == "validation" else ConflictError

    with pytest.raises(expected, match=message):
        await service.request(
            owner, PeopleIdentityRequest(**values), key=key, ceiling=Sensitivity.SENSITIVE
        )
    async with factory() as uow:
        assert await uow.people.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE) == alias
        events = await uow.events.list_after(session().id, 0, owner)
    assert [event for event in events if event.event_type == "people.identity_operation"] == []


async def test_identity_request_apply_requires_the_current_operation_revision() -> None:
    from agent_core.domain.people_views import PeopleIdentityRequest
    from tests.contract.support import session

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.write"}})
    first, second, alias = await _pair_with_alias(factory, owner)
    service = PeopleIdentityService(factory, clock, ids())
    preview = await service.request(
        owner,
        PeopleIdentityRequest(
            session_id=session().id,
            operation="merge",
            source_id=first.id,
            target_id=second.id,
            expected_revisions={first.id: 1, second.id: 1},
        ),
        key="preview",
        ceiling=Sensitivity.SENSITIVE,
    )

    with pytest.raises(ConflictError, match="operation revision changed"):
        await service.request(
            owner,
            PeopleIdentityRequest(
                session_id=session().id,
                operation="apply",
                operation_id=preview.id,
                expected_revision=preview.revision + 1,
            ),
            key="stale-apply",
            ceiling=Sensitivity.SENSITIVE,
        )
    async with factory() as uow:
        assert await uow.people.get(owner, alias.id, ceiling=Sensitivity.SENSITIVE) == alias


@pytest.mark.parametrize("operation", ["request", "merge", "split", "undo", "apply"])
async def test_identity_repair_requires_the_people_write_scope(operation: str) -> None:
    from agent_core.domain.errors import AuthorizationError
    from agent_core.domain.people_views import PeopleIdentityRequest
    from tests.contract.support import session

    clock, factory = await memory_uow_factory()
    writer = principal().model_copy(update={"scopes": {"people.write"}})
    reader = principal().model_copy(update={"scopes": {"people.read"}})
    first, second, alias = await _pair_with_alias(factory, writer)
    service = PeopleIdentityService(factory, clock, ids())
    revisions = {first.id: 1, second.id: 1}
    completed = await service.preview_merge(
        writer, first.id, second.id, expected_revisions=revisions, ceiling=Sensitivity.SENSITIVE
    )
    await service.apply(writer, completed.id, ceiling=Sensitivity.SENSITIVE)
    attempts = {
        "request": lambda: service.request(
            reader,
            PeopleIdentityRequest(
                session_id=session().id,
                operation="undo",
                operation_id=completed.id,
                expected_revision=completed.revision + 1,
            ),
            key="reader",
            ceiling=Sensitivity.SENSITIVE,
        ),
        "merge": lambda: service.preview_merge(
            reader, second.id, first.id, expected_revisions=revisions, ceiling=Sensitivity.SENSITIVE
        ),
        "split": lambda: service.preview_split(
            reader,
            second.id,
            first.id,
            selected_ids=[alias.id],
            expected_revisions=revisions,
            ceiling=Sensitivity.SENSITIVE,
        ),
        "undo": lambda: service.preview_undo(reader, completed.id, ceiling=Sensitivity.SENSITIVE),
        "apply": lambda: service.apply(reader, completed.id, ceiling=Sensitivity.SENSITIVE),
    }

    with pytest.raises(AuthorizationError, match=r"missing required scope: people\.write"):
        await attempts[operation]()
