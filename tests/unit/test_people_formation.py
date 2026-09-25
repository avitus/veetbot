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


async def test_owner_formation_links_known_people_with_a_directional_relationship() -> None:
    """Statements about people the owner knows link them, directionally and idempotently.

    ADR-0121: naming someone without tying them to the owner no longer creates
    them, so Maya and Jules are people the owner already knows here.
    """
    from datetime import timedelta

    clock, factory = await memory_uow_factory()
    known = await _seed_known(factory, "Maya", "Jules")
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
                source_timezone=None,
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
        assert {person.id for person in people.values()} == {p.id for p in known.values()}
        relationships = await uow.people.query(query.model_copy(update={"kinds": ["relationship"]}))
        assert len(relationships) == 1
        relation = relationships[0]
        assert isinstance(relation, RelationshipAssertion)
        assert relation.subject.id == people["Jules"].id and relation.object.id == people["Maya"].id
        assert relation.belief_id == result.beliefs[0].id
    await service.run(trigger="idle", scope="user", session_id=SESSION_ID, since_watermark=0)
    async with factory() as uow:
        assert len(await uow.people.query(query)) == 2


def _owner_kin_candidate(
    text: str,
    *,
    sequence: int,
    span: str,
    display_name: str,
    identifier_kind: str,
    identifier_value: str,
    context: str,
    predicate: str,
    qualifier: str,
) -> MemoryCandidate:
    return MemoryCandidate(
        belief_type=BeliefType.RELATIONSHIP,
        subject=display_name,
        statement=text,
        source_event_ids=[sequence],
        model_confidence=0.9,
        proposed_scope="user",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
        claim_kind=MemoryClaimKind.RELATIONSHIP,
        people=PeopleClaim(
            organizations=[],
            mentions=[
                PersonEvidence(
                    key="kin",
                    source_event_id=sequence,
                    start=text.index(span),
                    end=text.index(span) + len(span),
                    text=span,
                    display_name=display_name,
                    identifier_kind=identifier_kind,  # type: ignore[arg-type]
                    identifier_value=identifier_value,
                    namespace="owner",
                    context=context,
                    role="subject",
                    referent_key=None,
                )
            ],
            relationship=RelationshipProposal(
                subject_key="kin",
                object_key="owner",
                predicate=predicate,  # type: ignore[arg-type]
                qualifier=qualifier,
                valid_from=None,
                valid_to=None,
                precision="unknown",
                source_timezone=None,
            ),
            commitment=None,
        ),
    )


async def test_paraphrased_mention_context_does_not_lose_a_named_parent() -> None:
    """A "My mom, Cheryl" mention with context "User's mother" still forms and links Cheryl."""

    clock, factory = await memory_uow_factory()
    text = "My mom, Cheryl, lives in an independent living facility in Redwood City."
    sequence = await user_event(factory, text)
    candidate = _owner_kin_candidate(
        text,
        sequence=sequence,
        span="Cheryl",
        display_name="Cheryl",
        identifier_kind="name",
        identifier_value="Cheryl",
        context="User's mother",
        predicate="parent",
        qualifier="mother",
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
    assert [belief.statement for belief in result.beliefs] == [text]
    assert result.run.decision_counts.get("rejected_people_evidence", 0) == 0
    query = PeopleQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        sensitivity_ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        people = [r for r in await uow.people.query(query) if isinstance(r, Person)]
        assert [person.display_name for person in people] == ["Cheryl"]
        relationships = await uow.people.query(query.model_copy(update={"kinds": ["relationship"]}))
        assert len(relationships) == 1
        relation = relationships[0]
        assert isinstance(relation, RelationshipAssertion)
        assert relation.subject.id == people[0].id and relation.object.kind == "owner"
        assert relation.predicate == "parent" and relation.belief_id == result.beliefs[0].id
        links = await uow.people.query(query.model_copy(update={"kinds": ["memory_link"]}))
        assert [link.person_id for link in links if isinstance(link, PersonMemoryLink)] == [
            people[0].id
        ]


async def test_unnamed_relative_is_displayed_by_its_source_span() -> None:
    """A role mention labelled "User's brother" keeps the span "My brother" as its name."""

    clock, factory = await memory_uow_factory()
    text = "My brother lives in Redwood City."
    sequence = await user_event(factory, text)
    candidate = _owner_kin_candidate(
        text,
        sequence=sequence,
        span="My brother",
        display_name="User's brother",
        identifier_kind="role",
        identifier_value="brother",
        context="",
        predicate="sibling",
        qualifier="brother",
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
    assert [belief.statement for belief in result.beliefs] == [text]
    query = PeopleQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        sensitivity_ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        people = [r for r in await uow.people.query(query) if isinstance(r, Person)]
        assert [person.display_name for person in people] == ["My brother"]
        relationships = await uow.people.query(query.model_copy(update={"kinds": ["relationship"]}))
        assert len(relationships) == 1 and isinstance(relationships[0], RelationshipAssertion)
        assert relationships[0].predicate == "sibling"


async def test_lowercased_name_label_persists_the_source_casing() -> None:
    """A label spelt "cheryl" for the span "Cheryl" persists only the span's own casing."""

    from agent_core.domain.people import PersonIdentifier

    clock, factory = await memory_uow_factory()
    text = "My mom, Cheryl, lives in Redwood City."
    sequence = await user_event(factory, text)
    candidate = _owner_kin_candidate(
        text,
        sequence=sequence,
        span="Cheryl",
        display_name="cheryl",
        identifier_kind="name",
        identifier_value="cheryl",
        context="",
        predicate="parent",
        qualifier="mother",
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
    assert [belief.statement for belief in result.beliefs] == [text]
    query = PeopleQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        sensitivity_ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        rows = await uow.people.query(query)
        identifiers = await uow.people.query(
            query.model_copy(
                update={"kinds": ["identifier"], "sensitivity_ceiling": Sensitivity.RESTRICTED}
            )
        )
    assert [r.display_name for r in rows if isinstance(r, Person)] == ["Cheryl"]
    assert [r.value for r in identifiers if isinstance(r, PersonIdentifier)] == ["Cheryl"]


def test_source_casing_survives_case_folding_that_changes_length() -> None:
    from agent_core.memory.people_formation import _source_cased

    # "İ" folds to two code points, so a folded index is not a source index.
    assert _source_cased("İCheryl and Riv", "cheryl") == "Cheryl"
    assert _source_cased("Meet Straße Cheryl", "STRASSE") == "Straße"
    assert _source_cased("My mom, Cheryl", "CHERYL") == "Cheryl"
    assert _source_cased("My mom, Cheryl", "Cheryl") == "Cheryl"
    assert _source_cased("My brother", "User's brother") is None


async def test_invented_name_label_keeps_the_atomic_belief_unlinked() -> None:
    """A name label the source does not support drops the link, never the belief."""

    clock, factory = await memory_uow_factory()
    text = "My mom lives in an independent living facility in Redwood City."
    sequence = await user_event(factory, text)
    candidate = _owner_kin_candidate(
        text,
        sequence=sequence,
        span="My mom",
        display_name="Cheryl",
        identifier_kind="name",
        identifier_value="Cheryl",
        context="",
        predicate="parent",
        qualifier="mother",
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
    assert [belief.statement for belief in result.beliefs] == [text]
    assert result.beliefs[0].subject == "Cheryl", "the atomic belief keeps its own subject"
    assert result.run.decision_counts.get("rejected_people_evidence") == 1
    assert result.run.decision_counts.get("people_unlinked") == 1
    query = PeopleQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
    )
    async with factory() as uow:
        assert await uow.people.query(query) == [], "no People rows form from refused evidence"


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
            source_timezone=None,
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
        source_timezone=None,
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


async def test_explicit_people_reference_is_governed_like_remember_at_memory_trust() -> None:
    """A person-linked remember follows the base tool's trust and portability rules.

    Production refused "Erin is my wife" three times with tool.trust_rejected:
    a turn whose context holds recalled memory runs at memory trust, which the
    base tool accepts as an affirmed statement, but the person-linked path
    demanded user trust and so could never succeed for an owner with memories.
    The owner's message must still name each referenced person.
    """
    from dataclasses import replace
    from uuid import uuid4

    from agent_core.domain.errors import ToolTrustRejectedError
    from agent_core.domain.memory import MemoryAuthority
    from agent_core.domain.policies import TrustLevel
    from agent_core.tools.memory_remember import PeopleMemoryRememberTool
    from tests.contract.support import tool_context

    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    erin = Person(
        id=uuid4(),
        display_name="Erin",
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=clock.now(),
        updated_at=clock.now(),
    )
    async with factory() as uow:
        await uow.people.put(erin, expected_revision=0)
    await user_event(factory, "Kyrri and Riv are my daughters. Erin is my wife")
    service = GovernedMemoryService(factory, clock, ids(), owner, people_enabled=True)
    tool = PeopleMemoryRememberTool(service)
    # The production call, verbatim apart from the person identifier.
    arguments: dict[str, Any] = {
        "scope": "veetbot",
        "subject": "Erin",
        "statement": "Andy Vitus confirmed that Erin is his wife.",
        "belief_type": "relationship",
        "person_refs": [{"person_id": str(erin.id), "expected_revision": 1}],
        "portability": "portable",
    }
    recalled = replace(tool_context(), principal=owner, origin_trust=TrustLevel.MEMORY)
    refused = await tool.execute(arguments, recalled)
    assert not refused.ok and refused.failure is not None
    assert refused.failure.reason_code == "tool.invalid_arguments.portability_ceiling"
    assert refused.failure.retryable

    del arguments["portability"]
    untrusted = replace(tool_context(), principal=owner, origin_trust=TrustLevel.EXTERNAL_UNTRUSTED)
    with pytest.raises(ToolTrustRejectedError):
        await tool.execute(arguments, untrusted)
    assert await service.list_memories() == []

    result = await tool.execute(arguments, recalled)
    assert result.ok and result.structured is not None
    [belief] = await service.list_memories()
    assert belief.statement == "Andy Vitus confirmed that Erin is his wife."
    assert belief.authority is MemoryAuthority.AFFIRMED
    async with factory() as uow:
        links = await uow.people.query(
            PeopleQuery(
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                sensitivity_ceiling=Sensitivity.SENSITIVE,
                person_id=erin.id,
                kinds=["memory_link"],
            )
        )
    assert [link.belief_id for link in links if isinstance(link, PersonMemoryLink)] == [belief.id]


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
                    source_timezone=None,
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
    # ADR-0121: an affiliation links a person the owner already knows.
    await _seed_known(factory, "Maya")
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
            source_timezone=None,
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
                due_precision="unknown",
                source_timezone=None,
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
    assert [belief.statement for belief in result.beliefs] == [text], (
        "refused People evidence drops the link, not the atomic belief"
    )
    assert result.run.decision_counts.get("people_unlinked", 0) >= 1


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


# ---------------------------------------------------------------------------
# ADR-0121: People holds people the owner knows or interacts with.
# ---------------------------------------------------------------------------


class ManyExtractor:
    name = "people-fixture@1"

    def __init__(self, candidates: list[MemoryCandidate]) -> None:
        self.candidates = candidates

    async def extract(self, events: Any, **kwargs: Any) -> list[MemoryCandidate]:
        return list(self.candidates)


def _mention(
    text: str,
    sequence: int,
    span: str,
    key: str,
    *,
    kind: str = "name",
    value: str | None = None,
    role: str = "subject",
    referent: str | None = None,
    occurrence: int = 0,
) -> PersonEvidence:
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(span, start + 1)
    return PersonEvidence(
        key=key,
        source_event_id=sequence,
        start=start,
        end=start + len(span),
        text=span,
        display_name=span,
        identifier_kind=kind,  # type: ignore[arg-type]
        identifier_value=value or span,
        namespace="owner",
        context="",
        role=role,  # type: ignore[arg-type]
        referent_key=referent,
    )


def _claim_candidate(
    text: str,
    sequence: int,
    mentions: list[PersonEvidence],
    *,
    subject: str,
    relationship: tuple[str, str, str] | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        belief_type=BeliefType.RELATIONSHIP if relationship else BeliefType.FACT,
        subject=subject,
        statement=text,
        source_event_ids=[sequence],
        model_confidence=0.9,
        proposed_scope="user",
        proposed_portability=Portability.CONTEXTUAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
        claim_kind=(MemoryClaimKind.RELATIONSHIP if relationship else MemoryClaimKind.PROJECT_FACT),
        people=PeopleClaim(
            organizations=[],
            mentions=mentions,
            relationship=None
            if relationship is None
            else RelationshipProposal(
                subject_key=relationship[0],
                object_key=relationship[1],
                predicate=relationship[2],  # type: ignore[arg-type]
                qualifier="",
                valid_from=None,
                valid_to=None,
                precision="unknown",
                source_timezone=None,
            ),
            commitment=None,
        ),
    )


def _people_query(kinds: list[str]) -> PeopleQuery:
    return PeopleQuery(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        sensitivity_ceiling=Sensitivity.RESTRICTED,
        kinds=kinds,
    )


async def _seed_known(factory: Any, *names: str) -> dict[str, Person]:
    from uuid import uuid4

    from agent_core.domain.people import PersonIdentifier
    from tests.contract.support import NOW

    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    known = {}
    async with factory() as uow:
        for name in names:
            person = Person(id=uuid4(), display_name=name, state="active", **common)
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="name",
                    namespace="owner",
                    value=name,
                    context="owner",
                    verification="owner_confirmed",
                    valid_from=NOW,
                    **common,
                ),
                expected_revision=0,
            )
            known[name] = person
    return known


async def test_chat_mentions_without_an_owner_tie_link_existing_people_only() -> None:
    """Naming someone without tying them to the owner creates no one (ADR-0121)."""
    clock, factory = await memory_uow_factory()
    text = "Maya introduced Jules."
    sequence = await user_event(factory, text)
    candidate = _claim_candidate(
        text,
        sequence,
        [
            _mention(text, sequence, "Maya", "maya", role="object"),
            _mention(text, sequence, "Jules", "jules"),
        ],
        subject="introduction",
        relationship=("jules", "maya", "introduced_by"),
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=ManyExtractor([candidate]),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert [belief.statement for belief in result.beliefs] == [text]
    async with factory() as uow:
        assert await uow.people.query(_people_query(["person", "relationship"])) == []

    # Once the owner knows both people, the same kind of statement links them.
    clock2, factory2 = await memory_uow_factory()
    known = await _seed_known(factory2, "Maya", "Jules")
    sequence2 = await user_event(factory2, text)
    service2 = GovernedMemoryService(
        factory2,
        clock2,
        ids(),
        principal(),
        extractor=ManyExtractor(
            [
                _claim_candidate(
                    text,
                    sequence2,
                    [
                        _mention(text, sequence2, "Maya", "maya", role="object"),
                        _mention(text, sequence2, "Jules", "jules"),
                    ],
                    subject="introduction",
                    relationship=("jules", "maya", "introduced_by"),
                )
            ]
        ),
        policy_version="formation@11",
        people_enabled=True,
    )
    await service2.run(trigger="idle", scope="user", session_id=SESSION_ID)
    async with factory2() as uow:
        people = [
            r for r in await uow.people.query(_people_query(["person"])) if isinstance(r, Person)
        ]
        assert {person.id for person in people} == {known["Maya"].id, known["Jules"].id}
        [relation] = await uow.people.query(_people_query(["relationship"]))
        assert isinstance(relation, RelationshipAssertion)
        assert (relation.subject.id, relation.object.id) == (known["Jules"].id, known["Maya"].id)


async def test_owner_tie_creates_only_its_endpoint_and_keeps_the_relationship() -> None:
    """'My sister Maya ... her partner Jules' adds Maya, keeps her tie, and adds no Jules."""
    clock, factory = await memory_uow_factory()
    text = "My sister Maya is starting a bakery with her partner Jules."
    sequence = await user_event(factory, text)
    partner = _claim_candidate(
        text,
        sequence,
        [
            _mention(text, sequence, "Maya", "maya"),
            _mention(text, sequence, "Jules", "jules", role="object"),
        ],
        subject="Maya partner",
        relationship=("maya", "jules", "partner"),
    )
    sister = _claim_candidate(
        text,
        sequence,
        [_mention(text, sequence, "Maya", "maya")],
        subject="Maya",
        relationship=("maya", "owner", "sibling"),
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        # The untied claim comes first: the tie is recognized across the batch.
        extractor=ManyExtractor([partner, sister]),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert len(result.beliefs) == 2
    async with factory() as uow:
        people = [
            r for r in await uow.people.query(_people_query(["person"])) if isinstance(r, Person)
        ]
        assert [person.display_name for person in people] == ["Maya"]
        [relation] = await uow.people.query(_people_query(["relationship"]))
        assert isinstance(relation, RelationshipAssertion)
        assert relation.predicate == "sibling" and relation.object.kind == "owner"
        links = [
            row
            for row in await uow.people.query(_people_query(["memory_link"]))
            if isinstance(row, PersonMemoryLink)
        ]
        assert {link.belief_id for link in links} == {belief.id for belief in result.beliefs}
        assert all(link.person_id == people[0].id and not link.unresolved for link in links)
        kinship = next(
            b for b in result.beliefs if b.statement == text and b.subject.endswith(":Maya")
        )
        stored = await uow.memories.get(kinship.id, principal())
        assert stored.lifecycle_policy_version == "people-lifecycle@1"


async def test_pronouns_and_owner_self_references_never_create_or_link() -> None:
    """'You', the owner's own address, and handles are never people; 'she' still binds."""
    from agent_core.domain.email import EmailAccount, EmailRecord
    from tests.contract.support import NOW

    clock, factory = await memory_uow_factory()
    async with factory() as uow:
        await uow.email.put(
            EmailRecord(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kind="account",
                key="personal",
                revision=1,
                payload=EmailAccount(
                    id="personal",
                    label="Personal",
                    email_address="Avitus@Example.test",
                    status="syncing",
                ).model_dump(mode="json"),
                created_at=NOW,
                updated_at=NOW,
            ),
            expected_revision=0,
        )
    text = (
        "You are my friend. avitus@example.test is my brother. avitus is my cousin. "
        "My sister Maya said she is moving."
    )
    sequence = await user_event(factory, text)
    candidates = [
        _claim_candidate(
            text,
            sequence,
            [_mention(text, sequence, "You", "you")],
            subject="friend",
            relationship=("you", "owner", "friend"),
        ),
        _claim_candidate(
            text,
            sequence,
            [_mention(text, sequence, "avitus@example.test", "address", kind="email")],
            subject="brother",
            relationship=("address", "owner", "sibling"),
        ),
        _claim_candidate(
            text,
            sequence,
            [_mention(text, sequence, "avitus", "handle", kind="handle", occurrence=1)],
            subject="cousin",
            relationship=("handle", "owner", "relative"),
        ),
        _claim_candidate(
            text,
            sequence,
            [
                _mention(text, sequence, "Maya", "maya"),
                _mention(text, sequence, "she", "she", referent="maya"),
            ],
            subject="Maya",
            relationship=("maya", "owner", "sibling"),
        ),
    ]
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=ManyExtractor(candidates),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert len(result.beliefs) == 4, "every statement still forms as a belief"
    async with factory() as uow:
        people = [
            r for r in await uow.people.query(_people_query(["person"])) if isinstance(r, Person)
        ]
        assert [person.display_name for person in people] == ["Maya"]
        from agent_core.domain.people import PersonMention

        mentions = [
            row
            for row in await uow.people.query(_people_query(["mention"]))
            if isinstance(row, PersonMention)
        ]
        she = next(m for m in mentions if m.start == text.index(" she ") + 1)
        assert she.person_id == people[0].id


@pytest.mark.parametrize(
    "other",
    [
        "Nora met Sam for coffee.",
        "My sister Nora met Sam for coffee.",
        "Our sister Nora met Sam for coffee.",
    ],
)
async def test_reported_meeting_creates_first_person_participants_only(other: str) -> None:
    """'I met Maya' adds Maya; 'Nora met Sam' adds no one; bystanders are never added."""
    from agent_core.domain.memory import MemoryExtractionResult
    from agent_core.domain.people import PeopleInteraction
    from agent_core.domain.people_extraction import InteractionEvidence

    clock, factory = await memory_uow_factory()
    met = "I met Maya for lunch while Jules waited outside."
    first = await user_event(factory, met)
    second = await user_event(factory, other)
    interactions = [
        InteractionEvidence(
            source_event_id=first,
            text=met,
            summary=met,
            interaction_kind="meeting",
            occurred_at=None,
            precision="unknown",
            source_timezone=None,
            mentions=[
                _mention(met, first, "Maya", "maya", role="mentioned"),
                _mention(met, first, "Jules", "jules", role="mentioned"),
            ],
            participant_keys=["maya"],
        ),
        InteractionEvidence(
            source_event_id=second,
            text=other,
            summary=other,
            interaction_kind="meeting",
            occurred_at=None,
            precision="unknown",
            source_timezone=None,
            mentions=[
                _mention(other, second, "Nora", "nora", role="mentioned"),
                _mention(other, second, "Sam", "sam", role="mentioned"),
            ],
            participant_keys=["nora", "sam"],
        ),
    ]

    class InteractionExtractor:
        name = "interaction-fixture@1"

        async def extract(self, events: Any, **kwargs: Any) -> MemoryExtractionResult:
            return MemoryExtractionResult([], people_interactions=interactions)

    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=InteractionExtractor(),
        policy_version="formation@11",
        people_enabled=True,
    )
    await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    async with factory() as uow:
        people = [
            r for r in await uow.people.query(_people_query(["person"])) if isinstance(r, Person)
        ]
        assert [person.display_name for person in people] == ["Maya"]
        [history] = await uow.people.query(_people_query(["interaction"]))
        assert isinstance(history, PeopleInteraction)
        assert [p.person_id for p in history.participants] == [people[0].id]


async def test_group_named_meeting_participant_creates_no_person() -> None:
    """A team the owner met is not a person; the person met alongside it is (ADR-0125)."""
    from agent_core.domain.memory import MemoryExtractionResult
    from agent_core.domain.people_extraction import InteractionEvidence

    clock, factory = await memory_uow_factory()
    met = "I met the Investment Team and Dana today."
    sequence = await user_event(factory, met)
    interactions = [
        InteractionEvidence(
            source_event_id=sequence,
            text=met,
            summary=met,
            interaction_kind="meeting",
            occurred_at=None,
            precision="unknown",
            source_timezone=None,
            mentions=[
                _mention(met, sequence, "Investment Team", "team", role="mentioned"),
                _mention(met, sequence, "Dana", "dana", role="mentioned"),
            ],
            participant_keys=["team", "dana"],
        )
    ]

    class InteractionExtractor:
        name = "interaction-fixture@1"

        async def extract(self, events: Any, **kwargs: Any) -> MemoryExtractionResult:
            return MemoryExtractionResult([], people_interactions=interactions)

    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=InteractionExtractor(),
        policy_version="formation@11",
        people_enabled=True,
    )
    await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    async with factory() as uow:
        people = [
            r for r in await uow.people.query(_people_query(["person"])) if isinstance(r, Person)
        ]
    assert [person.display_name for person in people] == ["Dana"]


async def test_reinforced_belief_skips_projection_rows_erased_by_repair() -> None:
    """A belief formed again after the repair erased its People rows neither fails nor revives."""
    clock, factory = await memory_uow_factory()
    text = "My mom, Cheryl, lives in Redwood City."
    sequence = await user_event(factory, text)
    candidate = _owner_kin_candidate(
        text,
        sequence=sequence,
        span="Cheryl",
        display_name="Cheryl",
        identifier_kind="name",
        identifier_value="Cheryl",
        context="",
        predicate="parent",
        qualifier="mother",
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=ManyExtractor([candidate]),
        policy_version="formation@11",
        people_enabled=True,
    )
    await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    async with factory() as uow:
        erased = [
            row.id for row in await uow.people.query(_people_query(["relationship", "memory_link"]))
        ]
        assert len(erased) == 2
        await uow.people.erase(principal(), erased, preserve_independent=True)
    result = await service.run(
        trigger="idle", scope="user", session_id=SESSION_ID, since_watermark=0
    )
    assert result.run.decision_counts.get("rejected_people_evidence", 0) == 0
    async with factory() as uow:
        assert await uow.people.query(_people_query(["relationship", "memory_link"])) == []


@pytest.mark.parametrize("tie_source", ["wrong_scope", "assistant_claim", "assistant_interaction"])
async def test_untrusted_owner_tie_cannot_admit_a_chat_mention(tie_source: str) -> None:
    from agent_core.domain.events import NewEvent
    from agent_core.domain.memory import MemoryExtractionResult
    from agent_core.domain.people_extraction import InteractionEvidence

    clock, factory = await memory_uow_factory()
    text = "Maya likes coffee."
    sequence = await user_event(factory, text)
    claim = _claim_candidate(
        text, sequence, [_mention(text, sequence, "Maya", "maya")], subject="coffee"
    )
    tie_text = "I met Maya, my sister."
    if tie_source == "wrong_scope":
        tie_sequence = await user_event(factory, tie_text)
    else:
        async with factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=SESSION_ID,
                    run_id=None,
                    event_type="assistant.message.completed",
                    actor_type="assistant",
                    payload={"content": tie_text},
                )
            )
        tie_sequence = event.sequence
    tie = _claim_candidate(
        tie_text,
        tie_sequence,
        [_mention(tie_text, tie_sequence, "Maya", "maya")],
        subject="sister",
        relationship=("maya", "owner", "sibling"),
    )
    if tie_source == "wrong_scope":
        tie = tie.model_copy(update={"proposed_scope": "another-project"})
    interactions = []
    candidates = [claim, tie]
    if tie_source == "assistant_interaction":
        candidates = [claim]
        interactions = [
            InteractionEvidence(
                source_event_id=tie_sequence,
                text=tie_text,
                summary=tie_text,
                interaction_kind="meeting",
                occurred_at=None,
                precision="unknown",
                source_timezone=None,
                mentions=[_mention(tie_text, tie_sequence, "Maya", "maya")],
                participant_keys=["maya"],
            )
        ]

    class MixedExtractor:
        name = "mixed-owner-ties@1"

        async def extract(self, events: Any, **kwargs: Any) -> MemoryExtractionResult:
            return MemoryExtractionResult(candidates, people_interactions=interactions)

    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=MixedExtractor(),
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(trigger="idle", scope="user", session_id=SESSION_ID)
    assert any(belief.statement == text for belief in result.beliefs)
    async with factory() as uow:
        assert await uow.people.query(_people_query(["person"])) == []


@pytest.mark.parametrize("invalid", ["terminal_text", "source"])
async def test_unresolved_commitment_still_requires_admitted_state_evidence(invalid: str) -> None:
    from agent_core.domain.people_extraction import CommitmentProposal

    clock, factory = await memory_uow_factory()
    text = "Maya drafted an agenda for Jules but has not sent it."
    sequence = await user_event(factory, text)
    candidate = _claim_candidate(
        text,
        sequence,
        [
            _mention(text, sequence, "Maya", "maya"),
            _mention(text, sequence, "Jules", "jules", role="object"),
        ],
        subject="agenda",
    )
    assert candidate.people is not None
    candidate = candidate.model_copy(
        update={
            "people": candidate.people.model_copy(
                update={
                    "commitment": CommitmentProposal(
                        debtor_key="maya",
                        beneficiary_key="jules",
                        state="completed" if invalid == "terminal_text" else "proposed",
                        source_event_id=sequence if invalid == "terminal_text" else sequence + 100,
                        due_at=None,
                        due_precision="unknown",
                        source_timezone=None,
                    )
                }
            )
        }
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
    assert [belief.statement for belief in result.beliefs] == [text]
    assert result.run.decision_counts.get("rejected_people_evidence", 0) == 1
    async with factory() as uow:
        assert await uow.people.query(_people_query(["person", "commitment"])) == []
