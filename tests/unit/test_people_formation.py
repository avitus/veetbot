"""People formation grounds separate identities and directed facts in owner spans."""

from typing import Any

import pytest

from agent_core.domain.memory import (
    BeliefType,
    MemoryCandidate,
    MemoryClaimKind,
    Portability,
    Sensitivity,
)
from agent_core.domain.people import PeopleQuery, Person, PersonMemoryLink, RelationshipAssertion
from agent_core.domain.people_extraction import PeopleClaim, PersonEvidence, RelationshipProposal
from agent_core.memory.formation import GovernedMemoryService
from tests.contract.memory_fixtures import user_event
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import SESSION_ID, ids, memory_uow_factory, principal


class Extractor:
    name = "people-fixture@1"

    def __init__(self, candidate: MemoryCandidate) -> None:
        self.candidate = candidate

    async def extract(self, events: Any, **kwargs: Any) -> list[MemoryCandidate]:
        return [self.candidate]


async def test_commitment_formation_retains_supported_calendar_precision() -> None:
    from agent_core.domain.people import PeopleCommitment
    from agent_core.domain.people_extraction import CommitmentProposal
    from agent_core.domain.people_public import CommitmentView

    clock, factory = await memory_uow_factory()
    text = "Maya promised to send the report in August 2026, Los Angeles time."
    sequence = await user_event(factory, text)
    candidate = MemoryCandidate(
        belief_type=BeliefType.FACT,
        subject="Maya's report",
        statement=text,
        source_event_ids=[sequence],
        model_confidence=0.9,
        proposed_scope="user",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
        people=PeopleClaim(
            organizations=[],
            relationship=None,
            mentions=[
                PersonEvidence(
                    key="maya",
                    source_event_id=sequence,
                    start=0,
                    end=4,
                    text="Maya",
                    display_name="Maya",
                    identifier_kind="name",
                    identifier_value="Maya",
                    namespace="owner",
                    context="",
                    role="subject",
                    referent_key=None,
                )
            ],
            commitment=CommitmentProposal.model_validate(
                {
                    "debtor_key": "maya",
                    "beneficiary_key": "owner",
                    "state": "open",
                    "source_event_id": sequence,
                    "due_at": "2026-08-01T00:00:00-07:00",
                    "due_precision": "month",
                    "source_timezone": "America/Los_Angeles",
                }
            ),
        ),
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=Extractor(candidate),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert result.beliefs
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kinds=["commitment"],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
            )
        )
    assert len(rows) == 1 and isinstance(rows[0], PeopleCommitment)
    public = CommitmentView.model_validate(rows[0])
    assert public.due_precision == "month" and public.source_timezone == "America/Los_Angeles"
    assert public.due_at is not None and public.due_at.isoformat() == "2026-08-01T00:00:00-07:00"


async def test_owner_formation_creates_grounded_people_and_directional_link() -> None:
    from datetime import timedelta

    clock, factory = await memory_uow_factory()
    text = "Maya introduced Jules."
    sequence = await user_event(factory, text)
    mentions = [
        PersonEvidence(
            key=name.lower(),
            source_event_id=sequence,
            start=text.index(name),
            end=text.index(name) + len(name),
            text=name,
            display_name=name,
            identifier_kind="name",
            identifier_value=name,
            namespace="owner",
            context="",
            role="subject" if role == "subject" else "object",
            referent_key=None,
        )
        for name, role in (("Maya", "subject"), ("Jules", "object"))
    ]
    candidate = MemoryCandidate(
        belief_type=BeliefType.RELATIONSHIP,
        subject="introduction",
        statement=text,
        source_event_ids=[sequence],
        model_confidence=0.9,
        proposed_scope="user",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
        claim_kind=MemoryClaimKind.RELATIONSHIP,
        people=PeopleClaim(
            organizations=[],
            mentions=mentions,
            relationship=RelationshipProposal(
                subject_key="jules",
                object_key="maya",
                predicate="introduced_by",
                qualifier="",
                valid_from=None,
                valid_to=None,
                precision="unknown",
            ),
            commitment=None,
        ),
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=Extractor(candidate),
        policy_version="formation@11",
        people_enabled=True,
    )
    evidence_at = clock.now()
    clock.advance(timedelta(days=2))
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert result.beliefs
    assert result.beliefs[0].valid_from == evidence_at
    query = PeopleQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        sensitivity_ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        people = {r.display_name: r for r in await uow.people.query(query) if isinstance(r, Person)}
        assert set(people) == {"Maya", "Jules"}
        relationships = await uow.people.query(query.model_copy(update={"kinds": ["relationship"]}))
        assert len(relationships) == 1
        relation = relationships[0]
        assert isinstance(relation, RelationshipAssertion)
        assert relation.subject.id == people["Jules"].id and relation.object.id == people["Maya"].id
        assert relation.belief_id == result.beliefs[0].id
    await service.run(trigger="idle", scope="user", session_id=SESSION_ID, since_watermark=0)
    async with factory() as uow:
        assert len(await uow.people.query(query)) == 2


async def test_people_extractor_extends_only_new_policy_and_keeps_three_calls() -> None:
    import json

    import agent_core.memory.distillation as distillation
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.domain.messages import FakeModelScript, ResolvedModel, ScriptedTurn

    clock, factory = await memory_uow_factory()
    content = "My sister Maya enjoys chess."
    sequence = await user_event(factory, content)
    mention = PersonEvidence(
        key="maya",
        source_event_id=sequence,
        start=10,
        end=14,
        text="Maya",
        display_name="Maya",
        identifier_kind="name",
        identifier_value="Maya",
        namespace="owner",
        context="",
        role="subject",
        referent_key=None,
    )
    claim = PeopleClaim(
        organizations=[],
        mentions=[mention],
        relationship=RelationshipProposal(
            subject_key="maya",
            object_key="owner",
            predicate="sibling",
            qualifier="",
            valid_from=None,
            valid_to=None,
            precision="unknown",
        ),
        commitment=None,
    )
    response = {
        "candidates": [
            {
                "subject": "sister Maya",
                "statement": "User's sister is Maya.",
                "source_event_ids": [sequence],
                "sensitivity_guess": "sensitive",
                "claim_kind": "relationship",
                "derivation": "direct",
                "polarity": "assert",
                "evidence_spans": [{"source_event_id": sequence, "text": content}],
                "people": claim.model_dump(mode="json"),
            }
        ],
        "coverage": [
            {
                "coverage_unit_id": f"{sequence}:1",
                "decision": "formed",
                "candidate_indexes": [0],
                "prediction_indexes": [],
            }
        ],
        "interactions": [],
    }
    provider = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(text=text)
                for text in [
                    json.dumps(
                        {
                            "episodes": [
                                {
                                    "narrative": f"[e:{sequence}] {content}",
                                    "subjects": ["sister Maya"],
                                    "source_event_ids": [sequence],
                                }
                            ]
                        }
                    ),
                    '{"predictions":[]}',
                    json.dumps(response),
                ]
            ]
        ),
        clock,
    )
    extractor_class = getattr(distillation, "PeopleAssistedCandidateExtractor", None)
    assert extractor_class is not None, "formation@11 requires a separately versioned extractor"
    extractor = extractor_class(
        provider=provider,
        resolved_model=ResolvedModel(
            provider="fake", model="scripted", policy_name="people", resolved_at=clock.now()
        ),
        uow_factory=factory,
        clock=clock,
        ids=ids(),
    )
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, 0, principal())
    result = await extractor.extract(events, principal=principal(), scope="user")
    assert extractor.last_audit.provider_calls == 3
    assert any(candidate.people == claim for candidate in result)
    assert "people" not in distillation._DistilledCandidate.model_fields
    assert "interactions" not in distillation._DistillationResponse.model_fields


async def test_reported_interaction_forms_without_creating_an_atomic_belief() -> None:
    from agent_core.domain.memory import MemoryExtractionResult
    from agent_core.domain.people import PeopleInteraction
    from agent_core.domain.people_extraction import InteractionEvidence

    clock, factory = await memory_uow_factory()
    content = "I met Maya for lunch."
    sequence = await user_event(factory, content)
    mention = PersonEvidence(
        key="maya",
        source_event_id=sequence,
        start=6,
        end=10,
        text="Maya",
        display_name="Maya",
        identifier_kind="name",
        identifier_value="Maya",
        namespace="owner",
        context="",
        role="mentioned",
        referent_key=None,
    )
    interaction = InteractionEvidence(
        source_event_id=sequence,
        text=content,
        summary=content,
        interaction_kind="meeting",
        occurred_at=None,
        precision="unknown",
        mentions=[mention],
        participant_keys=["maya"],
    )

    class InteractionExtractor:
        name = "interaction-fixture@1"

        async def extract(self, events: Any, **kwargs: Any) -> MemoryExtractionResult:
            return MemoryExtractionResult([], people_interactions=[interaction])

    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=InteractionExtractor(),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert result.beliefs == []
    query = PeopleQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        sensitivity_ceiling=Sensitivity.SENSITIVE,
        kinds=["interaction"],
    )
    async with factory() as uow:
        history = await uow.people.query(query)
        assert len(history) == 1, "source-grounded history does not require a durable belief"
        row = history[0]
        assert isinstance(row, PeopleInteraction)
        assert row.attribution == "owner_reported" and row.occurred_at is None
    await service.run(trigger="idle", scope="user", session_id=SESSION_ID, since_watermark=0)
    async with factory() as uow:
        assert len(await uow.people.query(query)) == 1


async def test_explicit_remember_validates_people_revisions_before_writing() -> None:
    import pytest

    import agent_core.tools.memory_remember as tools
    from agent_core.domain.errors import ConflictError
    from tests.contract.support import tool_context

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    person = Person(
        id=__import__("uuid").uuid4(),
        display_name="Maya",
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
    await user_event(factory, "Remember that Maya enjoys chess.")
    service = GovernedMemoryService(factory, clock, ids(), owner, people_enabled=True)
    tool_class = getattr(tools, "PeopleMemoryRememberTool", None)
    assert tool_class is not None, "new remember version must validate optional person references"
    tool = tool_class(service)
    from dataclasses import replace

    context = replace(tool_context(), principal=owner)
    arguments: dict[str, Any] = {
        "statement": "Maya enjoys chess.",
        "subject": "chess",
        "scope": "user",
        "person_refs": [{"person_id": str(person.id), "expected_revision": 2, "role": "subject"}],
    }
    with pytest.raises(ConflictError):
        await tool.execute(arguments, context)
    assert await service.list_memories() == []
    arguments["person_refs"][0]["expected_revision"] = 1
    result = await tool.execute(arguments, context)
    assert result.ok
    async with factory() as uow:
        links = await uow.people.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                sensitivity_ceiling=Sensitivity.SENSITIVE,
                person_id=person.id,
                kinds=["memory_link"],
            )
        )
        assert len(links) == 1
        assert isinstance(links[0], PersonMemoryLink) and result.structured is not None
        assert str(links[0].belief_id) == result.structured["belief_id"]
    assert "person_refs" not in tools.MemoryRememberTool.spec.input_schema["properties"]


async def test_explicit_people_reference_does_not_match_a_name_inside_another_name() -> None:
    from dataclasses import replace
    from uuid import uuid4

    import pytest

    from agent_core.domain.errors import ToolValidationError
    from agent_core.tools.memory_remember import PeopleMemoryRememberTool
    from tests.contract.support import tool_context

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    person = Person(
        id=uuid4(),
        display_name="Ann",
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
    await user_event(factory, "Remember that Joanna enjoys chess.")
    service = GovernedMemoryService(factory, clock, ids(), owner, people_enabled=True)
    tool = PeopleMemoryRememberTool(service)
    with pytest.raises(ToolValidationError, match="identify"):
        await tool.execute(
            {
                "statement": "Ann enjoys chess.",
                "subject": "chess",
                "scope": "user",
                "person_refs": [{"person_id": str(person.id), "expected_revision": 1}],
            },
            replace(tool_context(), principal=owner),
        )
    assert await service.list_memories() == []


async def test_explicit_people_reference_accepts_confirmed_alias_but_rejects_collision() -> None:
    from dataclasses import replace
    from uuid import uuid4

    import pytest

    from agent_core.domain.errors import ToolValidationError
    from agent_core.domain.people import PersonIdentifier
    from agent_core.tools.memory_remember import PeopleMemoryRememberTool
    from tests.contract.support import tool_context

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": clock.now(),
        "updated_at": clock.now(),
    }
    person = Person(id=uuid4(), display_name="Anne Smith", **common)
    alias = PersonIdentifier(
        id=uuid4(),
        person_id=person.id,
        identifier_kind="name",
        value="Annie",
        namespace="owner",
        context="owner",
        verification="owner_confirmed",
        valid_from=clock.now(),
        **common,
    )
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(alias, expected_revision=0)
    await user_event(factory, "Remember that Annie enjoys chess.")
    service = GovernedMemoryService(factory, clock, ids(), owner, people_enabled=True)
    tool = PeopleMemoryRememberTool(service)
    arguments = {
        "statement": "Annie enjoys chess.",
        "subject": "chess",
        "scope": "user",
        "person_refs": [{"person_id": str(person.id), "expected_revision": 1}],
    }
    context = replace(tool_context(), principal=owner)
    assert (await tool.execute(arguments, context)).ok
    async with factory() as uow:
        await uow.people.put(
            Person(id=uuid4(), display_name="Annie", **common), expected_revision=0
        )
    with pytest.raises(ToolValidationError, match="identify"):
        await tool.execute(arguments, context)


@pytest.mark.parametrize(
    "policy, enabled",
    [
        ("formation@11", True),
        ("formation@11", False),
        ("formation@9", False),
        ("formation@1", False),
    ],
)
async def test_erasure_during_extraction_fences_every_candidate_before_commit(
    policy: str,
    enabled: bool,
) -> None:
    from agent_core.domain.people import PeopleSource
    from agent_core.memory.people_formation import source_id

    clock, factory = await memory_uow_factory()
    sequence = await user_event(factory, "Maya lives in Canada.")
    sid = source_id(principal(), SESSION_ID, sequence)
    async with factory() as uow:
        await uow.people.put(
            PeopleSource(
                id=sid,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                session_id=SESSION_ID,
                event_sequence=sequence,
                evidence_at=clock.now(),
                source_kind="owner",
                source_revision="1",
                created_at=clock.now(),
                updated_at=clock.now(),
            ),
            expected_revision=0,
        )
    candidate = MemoryCandidate(
        belief_type=BeliefType.FACT,
        subject="Maya residence",
        statement="Maya lives in Canada.",
        source_event_ids=[sequence],
        model_confidence=0.6,
        proposed_scope="user",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
    )

    class RacingExtractor(Extractor):
        async def extract(self, events: Any, **kwargs: Any) -> list[MemoryCandidate]:
            async with factory() as uow, uow.people.lock(principal()):
                await uow.people.erase(principal(), [sid])
            return [candidate]

    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=RacingExtractor(candidate),
        policy_version=policy,
        people_enabled=enabled,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert result.beliefs == [], "a proposal prepared before erasure cannot recreate derived memory"


@pytest.mark.parametrize("explicit_source", [True, False])
async def test_explicit_memory_cannot_reuse_a_forgotten_source(explicit_source: bool) -> None:
    from agent_core.domain.errors import ConflictError
    from agent_core.domain.people import PeopleSource
    from agent_core.domain.people_sources import source_id

    clock, factory = await memory_uow_factory()
    sequence = await user_event(factory, "Maya lives in Canada.")
    source = PeopleSource(
        id=source_id(principal(), SESSION_ID, sequence),
        session_id=SESSION_ID,
        event_sequence=sequence,
        evidence_at=clock.now(),
        source_kind="owner",
        source_revision="1",
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    async with factory() as uow:
        await uow.people.put(source, expected_revision=0)
        await uow.people.erase(principal(), [source.id])
    service = GovernedMemoryService(factory, clock, ids(), principal())
    with pytest.raises(ConflictError, match=r"source.*erased"):
        await service.remember(
            session_id=SESSION_ID,
            run_id=None,
            statement="Maya resides in Canada.",
            subject="Maya residence",
            scope="user",
            source_event_ids=[sequence] if explicit_source else None,
        )
    async with factory() as uow:
        assert await uow.memories.list_memories(principal()) == []
    fresh_sequence = await user_event(factory, "I enjoy cycling.")
    fresh = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User enjoys cycling.",
        subject="cycling",
        scope="user",
        source_event_ids=[fresh_sequence],
    )
    assert fresh.source_event_ids == [fresh_sequence]


async def test_only_direct_owner_kinship_escapes_idle_decay() -> None:
    from datetime import timedelta

    from agent_core.domain.memory import MemoryDerivation, MemoryLongevity

    for predicate in ("sibling", "spouse"):
        clock, factory = await memory_uow_factory()
        content = "Maya is my sister." if predicate == "sibling" else "Maya is my spouse."
        sequence = await user_event(factory, content)
        candidate = MemoryCandidate(
            belief_type=BeliefType.RELATIONSHIP,
            subject="Maya relationship",
            statement=content,
            source_event_ids=[sequence],
            model_confidence=0.8,
            proposed_scope="user",
            proposed_portability=Portability.CONTEXTUAL,
            sensitivity_guess=Sensitivity.SENSITIVE,
            claim_kind=MemoryClaimKind.RELATIONSHIP,
            derivation=MemoryDerivation.DIRECT,
            longevity=MemoryLongevity.DURABLE,
            people=PeopleClaim(
                organizations=[],
                mentions=[
                    PersonEvidence(
                        key="maya",
                        source_event_id=sequence,
                        start=0,
                        end=4,
                        text="Maya",
                        display_name="Maya",
                        identifier_kind="name",
                        identifier_value="Maya",
                        namespace="owner",
                        context="",
                        role="subject",
                        referent_key=None,
                    )
                ],
                relationship=RelationshipProposal(
                    subject_key="maya",
                    object_key="owner",
                    predicate=predicate,
                    qualifier="",
                    valid_from=None,
                    valid_to=None,
                    precision="unknown",
                ),
                commitment=None,
            ),
        )
        service = GovernedMemoryService(
            factory,
            clock,
            ids(),
            principal(),
            extractor=Extractor(candidate),
            policy_version="formation@11",
            people_enabled=True,
        )
        result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
        assert len(result.beliefs) == 1
        original = result.beliefs[0]
        clock.advance(timedelta(days=800))
        await service.decay()
        async with factory() as uow:
            stored = await uow.memories.get(original.id, principal())
        if predicate == "sibling":
            assert stored.confidence == original.confidence
            assert stored.lifecycle_policy_version == "people-lifecycle@1"
            assert stored.expires_at is None
        else:
            assert stored.confidence < original.confidence
            assert stored.lifecycle_policy_version != "people-lifecycle@1"


async def test_affiliation_uses_organization_endpoint_and_preserves_source() -> None:
    from agent_core.domain.people import OrganizationReference
    from agent_core.domain.people_extraction import OrganizationEvidence

    clock, factory = await memory_uow_factory()
    content = "Maya works at Acme."
    sequence = await user_event(factory, content)
    claim = PeopleClaim(
        mentions=[
            PersonEvidence(
                key="maya",
                source_event_id=sequence,
                start=0,
                end=4,
                text="Maya",
                display_name="Maya",
                identifier_kind="name",
                identifier_value="Maya",
                namespace="owner",
                context="",
                role="subject",
                referent_key=None,
            )
        ],
        organizations=[
            OrganizationEvidence(
                key="acme",
                source_event_id=sequence,
                start=14,
                end=18,
                text="Acme",
                display_name="Acme",
            )
        ],
        relationship=RelationshipProposal(
            subject_key="maya",
            object_key="acme",
            predicate="employment",
            qualifier="",
            valid_from=None,
            valid_to=None,
            precision="unknown",
        ),
        commitment=None,
    )
    candidate = MemoryCandidate(
        belief_type=BeliefType.RELATIONSHIP,
        subject="Maya work",
        statement=content,
        source_event_ids=[sequence],
        model_confidence=0.8,
        proposed_scope="user",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
        claim_kind=MemoryClaimKind.RELATIONSHIP,
        people=claim,
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=Extractor(candidate),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert len(result.beliefs) == 1
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                sensitivity_ceiling=Sensitivity.SENSITIVE,
                kinds=["organization", "relationship"],
            )
        )
    organization = next(row for row in rows if isinstance(row, OrganizationReference))
    relation = next(row for row in rows if isinstance(row, RelationshipAssertion))
    assert relation.object.kind == "organization" and relation.object.id == organization.id
    assert organization.support_ids == relation.support_ids


async def test_a_draft_cannot_complete_a_person_commitment() -> None:
    from agent_core.domain.people_extraction import CommitmentProposal

    clock, factory = await memory_uow_factory()
    text = "I drafted an agenda for Maya, but I have not sent it."
    sequence = await user_event(factory, text)
    mention = PersonEvidence(
        key="maya",
        source_event_id=sequence,
        start=text.index("Maya"),
        end=text.index("Maya") + 4,
        text="Maya",
        display_name="Maya",
        identifier_kind="name",
        identifier_value="Maya",
        namespace="owner",
        context="",
        role="object",
        referent_key=None,
    )
    candidate = MemoryCandidate(
        belief_type=BeliefType.FACT,
        subject="agenda",
        statement=text,
        source_event_ids=[sequence],
        model_confidence=0.9,
        proposed_scope="user",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
        people=PeopleClaim(
            organizations=[],
            mentions=[mention],
            relationship=None,
            commitment=CommitmentProposal(
                debtor_key="owner",
                beneficiary_key="maya",
                state="completed",
                source_event_id=sequence,
                due_at=None,
            ),
        ),
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=Extractor(candidate),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    async with factory() as uow:
        commitments = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kinds=["commitment"],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
            )
        )
    assert commitments == [], "drafting and negated delivery cannot establish completion"
    assert result.run.decision_counts.get("rejected_people_evidence", 0) >= 1


def test_completed_commitment_requires_affirmative_source_language() -> None:
    import pytest

    from agent_core.domain.errors import ToolValidationError
    from agent_core.memory.people_formation import validate_commitment_state

    validate_commitment_state("completed", "Maya sent the promised agenda.")
    validate_commitment_state("cancelled", "We cancelled the meeting with Maya.")
    for text in [
        "Maya has not sent the agenda.",
        "Maya might send it.",
        "Maya is preparing the agenda.",
    ]:
        with pytest.raises(ToolValidationError):
            validate_commitment_state("completed", text)


@pytest.mark.parametrize(
    "extractor_name", ["NemoriAssistedCandidateExtractor", "PeopleAssistedCandidateExtractor"]
)
@pytest.mark.parametrize("erase_before", [False, True])
async def test_episode_extraction_honors_erasure_before_and_during_provider(
    extractor_name: str,
    erase_before: bool,
) -> None:
    import json

    import agent_core.memory.distillation as distillation
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.domain.messages import FakeModelScript, ResolvedModel
    from agent_core.domain.people import PeopleSource
    from agent_core.domain.people_sources import source_id

    clock, factory = await memory_uow_factory()
    sequence = await user_event(factory, "Maya lives in Canada.")
    sid = source_id(principal(), SESSION_ID, sequence)
    async with factory() as uow:
        await uow.people.put(
            PeopleSource(
                id=sid,
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                session_id=SESSION_ID,
                event_sequence=sequence,
                evidence_at=clock.now(),
                source_kind="owner",
                source_revision="1",
                created_at=clock.now(),
                updated_at=clock.now(),
            ),
            expected_revision=0,
        )
        events = await uow.events.list_after(SESSION_ID, 0, principal())
        if erase_before:
            await uow.people.erase(principal(), [sid])
    extractor = getattr(distillation, extractor_name)(
        provider=FakeModelProvider(FakeModelScript(turns=[]), clock),
        resolved_model=ResolvedModel(
            provider="fake", model="scripted", policy_name="fixture", resolved_at=clock.now()
        ),
        uow_factory=factory,
        clock=clock,
        ids=ids(),
    )
    calls = []

    async def racing_call(**kwargs: Any) -> Any:
        calls.append(kwargs["stage"])
        async with factory() as uow, uow.people.lock(principal()):
            await uow.people.erase(principal(), [sid])
        response = {
            "episodes": [
                {
                    "narrative": f"[e:{sequence}] Maya lives in Canada.",
                    "subjects": ["Maya residence"],
                    "source_event_ids": [sequence],
                }
            ]
        }
        return json.dumps(response), None, distillation._empty_stage_metric("completed")

    extractor._call = racing_call
    await extractor.extract(events, principal=principal(), scope="user")
    async with factory() as uow:
        assert await uow.episodes.for_session(SESSION_ID, principal()) == []
    assert calls == ([] if erase_before else ["episode_integration"])
