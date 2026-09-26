"""Synthetic sources exercise the production People Email adapter and its boundaries."""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from agent_core.domain.email_people import EmailPeopleFact
from agent_core.domain.memory import BeliefType, MemoryAuthority, Sensitivity
from agent_core.domain.people import (
    PeopleQuery,
    PeopleSource,
    Person,
    PersonIdentifier,
    PersonMemoryLink,
    RelationshipAssertion,
)
from agent_core.domain.people_extraction import PeopleClaim, PersonEvidence, RelationshipProposal
from agent_core.memory.email_people import EmailPeopleFormationService
from agent_core.ports.persistence import RepositoryUnitOfWork
from tests.contract.memory_fixtures import semantic_stack
from tests.contract.people_fixtures import PeopleFields
from tests.contract.people_mail_fixtures import (
    ALEX,
    OWNER,
    all_rows,
    colleague_fact,
    correspondence_stack,
    mail,
    seed_account,
)
from tests.contract.support import NOW, ids, principal


@pytest.mark.parametrize("state", ["proposed", "open", "completed", "cancelled", "uncertain"])
@pytest.mark.parametrize("draft", [True, False])
async def test_unsent_email_draft_cannot_establish_a_committed_action(
    state: str, draft: bool
) -> None:
    from agent_core.domain.errors import ToolValidationError

    body = {
        "proposed": "Alex proposed sending the agenda to Maya.",
        "open": "Alex promised to send the agenda to Maya.",
        "completed": "Alex completed the agenda for Maya.",
        "cancelled": "Alex cancelled the agenda for Maya.",
        "uncertain": "Alex may send the agenda to Maya.",
    }[state]
    factory, legacy, source, _, _ = await semantic_stack(
        body=body, label_ids=["DRAFT"] if draft else ["INBOX"]
    )
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    # ADR-0121: mail links people the owner already knows and never adds them.
    await _seed_sender_context_people(factory, source, "Alex", "Maya")
    fact = EmailPeopleFact.model_validate(
        {
            "message_id": source.message_id,
            "quote": body,
            "belief_type": "fact",
            "subject": "Alex agenda",
            "predicate": "completed",
            "value": "agenda",
            "people": {
                "mentions": [
                    {
                        "key": name.lower(),
                        "source_event_id": 1,
                        "start": body.index(name),
                        "end": body.index(name) + len(name),
                        "text": name,
                        "display_name": name,
                        "identifier_kind": "name",
                        "identifier_value": name,
                        "namespace": "owner",
                        "context": "",
                        "role": "subject" if name == "Alex" else "object",
                        "referent_key": None,
                    }
                    for name in ("Alex", "Maya")
                ],
                "organizations": [],
                "relationship": None,
                "commitment": {
                    "debtor_key": "alex",
                    "beneficiary_key": "maya",
                    "state": state,
                    "source_event_id": 1,
                    "due_at": None,
                    "due_precision": "unknown",
                    "source_timezone": None,
                },
            },
        }
    )
    if draft and state in {"open", "completed", "cancelled"}:
        with pytest.raises(ToolValidationError, match="draft"):
            await service.form(source, [fact])
    else:
        assert len(await service.form(source, [fact])) == 1
        from agent_core.domain.people import PeopleCommitment
        from agent_core.domain.people_sources import email_source_id

        async with factory() as uow:
            rows = await uow.people.query(
                PeopleQuery(
                    tenant_id=principal().tenant_id,
                    principal_id=principal().principal_id,
                    kinds=["commitment"],
                    sensitivity_ceiling=Sensitivity.SENSITIVE,
                )
            )
        assert len(rows) == 1 and isinstance(rows[0], PeopleCommitment)
        assert rows[0].state_source_id == email_source_id(principal(), source)
        assert rows[0].state == state


async def test_import_recovers_only_revision_bound_original_passages() -> None:
    from datetime import timedelta

    import pytest

    from agent_core.domain.email_semantics import semantic_source_key
    from agent_core.domain.errors import ToolTrustRejectedError

    factory, legacy, source, _, _ = await semantic_stack(age=200)
    await legacy.register_source(source)

    async def guard(uow: RepositoryUnitOfWork) -> None:
        return None

    service = EmailPeopleFormationService(
        factory,
        legacy._clock,
        ids(),
        principal(),
        provider="fake",
        model="scripted",
        import_window=(source.sent_at, source.sent_at + timedelta(days=1)),
        import_guard=guard,
    )
    async with factory() as uow:
        record = await uow.email.get(
            principal(),
            "semantic_source",
            semantic_source_key(source.account_id, source.provider_thread_id, source.message_id),
        )
    assert record is not None
    recovered = await service.import_passage(record, after_offset=None)
    assert recovered == source
    assert await service.import_passage(record, after_offset=source.body_offset) is None
    changed = record.model_copy(
        update={
            "payload": {
                **record.payload,
                "passages": {"0": "0" * 64},
            }
        }
    )
    with pytest.raises(ToolTrustRejectedError, match="fingerprint"):
        await service.import_passage(changed, after_offset=None)


async def _seed_sender_context_people(factory: Any, source: Any, *names: str) -> list[Person]:
    """People the owner knows, named as this message's sender names them."""
    import hashlib

    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW - timedelta(days=90),
        "updated_at": NOW - timedelta(days=90),
    }
    people = []
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
                    context="email:" + hashlib.sha256(source.sender.encode()).hexdigest(),
                    verification="contextual",
                    valid_from=NOW - timedelta(days=90),
                    **common,
                ),
                expected_revision=0,
            )
            people.append(person)
    return people


async def test_email_people_reuses_source_and_keeps_attributed_tentative_authority() -> None:
    factory, legacy, source, _fact, _ = await semantic_stack(
        age=3, body="Alex is Maya's colleague."
    )
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    mentions = [
        PersonEvidence(
            key=name.casefold(),
            source_event_id=1,
            start=source.body.index(name),
            end=source.body.index(name) + len(name),
            text=name,
            display_name=name,
            identifier_kind="name",
            identifier_value=name,
            namespace="owner",
            context="",
            role="subject" if role == "subject" else "object",
            referent_key=None,
        )
        for name, role in (("Alex", "subject"), ("Maya", "object"))
    ]
    fact = EmailPeopleFact(
        message_id=source.message_id,
        quote=source.body,
        belief_type=BeliefType.RELATIONSHIP,
        subject="Alex relationship",
        predicate="is",
        value="Maya's colleague",
        people=PeopleClaim(
            mentions=mentions,
            organizations=[],
            relationship=RelationshipProposal(
                subject_key="alex",
                object_key="maya",
                predicate="colleague",
                qualifier="",
                valid_from=None,
                valid_to=None,
                precision="unknown",
                source_timezone=None,
            ),
            commitment=None,
        ),
    )
    result = await service.form(source, [fact])
    assert len(result) == 1
    belief = result[0]
    assert belief.consolidation_policy_version == "email-semantic@2"
    assert belief.authority is MemoryAuthority.INFERRED and belief.confidence <= 0.4
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kinds=["person", "source", "relationship", "identifier"],
                sensitivity_ceiling=Sensitivity.SENSITIVE,
            )
        )
    # ADR-0121: names in a message body never add anyone to People, so no
    # person, alias, or relationship forms; the attributed fact still does.
    assert not [row for row in rows if isinstance(row, (Person, PersonIdentifier))]
    assert not [row for row in rows if isinstance(row, RelationshipAssertion)]
    assert belief.subject.startswith("person:unresolved:")
    evidence = next(row for row in rows if isinstance(row, PeopleSource))
    assert evidence.source_kind == "email" and evidence.message_id == source.message_id
    assert evidence.account_id == source.account_id and evidence.evidence_at == source.sent_at
    assert await service.form(source, [fact]) == result
    await service.exclude_source(source.account_id, source.provider_thread_id, source.message_id)
    async with factory() as uow:
        assert (
            await uow.people.query(
                PeopleQuery(
                    tenant_id=principal().tenant_id,
                    principal_id=principal().principal_id,
                    kinds=["relationship", "source", "mention", "memory_link"],
                    sensitivity_ceiling=Sensitivity.SENSITIVE,
                )
            )
            == []
        )


async def test_email_people_satisfies_shared_authority_contract_and_keeps_older_mail_out() -> None:
    from tests.contract.test_email_semantic_port_contract import assert_email_semantic_port

    factory, legacy, source, fact, _ = await semantic_stack(age=3)
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    await assert_email_semantic_port(service, source, fact)
    factory, legacy, source, fact, _ = await semantic_stack(age=91)
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    assert await service.form(source, [fact]) == []


@pytest.mark.parametrize(
    "headers, direction",
    [
        ({}, "incoming"),
        ({"label_ids": ["DRAFT"]}, None),
        (
            {
                "from": "Owner <owner@example.test>",
                "to": "Alex <alex@example.test>",
                "label_ids": ["SENT"],
            },
            "outgoing",
        ),
        (
            {
                "from": "Owner <owner@example.test>",
                "to": "Alex <alex@example.test>",
                "label_ids": ["DRAFT"],
            },
            None,
        ),
        ({"from": "Owner <owner@example.test>", "to": "Alex <alex@example.test>"}, None),
    ],
)
@pytest.mark.parametrize("status", ["ready", "syncing", "unavailable"])
async def test_email_headers_link_confirmed_sender_to_observed_history(
    headers: dict[str, Any], direction: str | None, status: str
) -> None:
    from uuid import uuid4

    from agent_core.domain.email import EmailAccount, EmailRecord
    from agent_core.domain.people import PeopleInteraction, PersonIdentifier
    from tests.contract.support import NOW

    factory, legacy, source, _fact, _ = await semantic_stack(age=3, **headers)
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Alex", **common)
    # A refresh marks the account syncing before it registers mail (ADR-0121).
    account = EmailAccount(
        id="work",
        label="Work",
        email_address="owner@example.test",
        status=status,  # type: ignore[arg-type]
    )
    async with factory() as uow:
        await uow.email.put(
            EmailRecord(
                kind="account",
                key="work",
                revision=1,
                payload=account.model_dump(mode="json"),
                **common,
            ),
            expected_revision=0,
        )
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=person.id,
                identifier_kind="email",
                value="alex@example.test",
                namespace="owner",
                context="owner",
                verification="owner_confirmed",
                valid_from=source.sent_at,
                **common,
            ),
            expected_revision=0,
        )
    await service.register_source(source)
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kinds=["interaction"],
                sensitivity_ceiling=Sensitivity.SENSITIVE,
                person_id=person.id,
            )
        )
    if direction is None:
        assert rows == []
        return
    assert len(rows) == 1 and isinstance(rows[0], PeopleInteraction)
    assert (
        rows[0].channel == "email"
        and rows[0].attribution == "observed"
        and rows[0].direction == direction
    )
    assert rows[0].occurred_at == source.sent_at
    await service.register_source(source)
    async with factory() as uow:
        assert (
            len(
                await uow.people.query(
                    PeopleQuery(
                        tenant_id=principal().tenant_id,
                        principal_id=principal().principal_id,
                        kinds=["interaction"],
                        sensitivity_ceiling=Sensitivity.SENSITIVE,
                        person_id=person.id,
                    )
                )
            )
            == 1
        )


async def test_email_people_forms_without_evaluation_evidence() -> None:
    factory, legacy, source, fact, _ = await semantic_stack()
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    assert service.enabled and service.people_enabled
    await service.register_source(source)
    assert len(await service.form(source, [fact])) == 1


def test_people_assessment_schema_keeps_quote_offsets_local_and_old_schema_frozen() -> None:
    from agent_core.domain.email import EmailAssessment
    from agent_core.domain.email_people import EmailPeopleAssessment
    from agent_core.runtime.email_tasks import _response_schema

    old = _response_schema(EmailAssessment)
    new = _response_schema(EmailPeopleAssessment)
    assert "people_facts" not in old["properties"]
    assert new["$defs"]["PersonEvidence"]["properties"]["source_event_id"] == {
        "type": "integer",
        "const": 1,
    }
    for definition in [new, *new["$defs"].values()]:
        if definition.get("type") == "object":
            assert set(definition["required"]) == set(definition["properties"])


async def test_old_email_requires_an_explicit_date_window_and_active_import_guard() -> None:
    from datetime import timedelta

    factory, legacy, source, old_fact, _ = await semantic_stack(age=200)
    ordinary = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    assert await ordinary.form(source, [old_fact]) == []
    guarded = []

    async def guard(uow: RepositoryUnitOfWork) -> None:
        guarded.append(True)

    imported = EmailPeopleFormationService(
        factory,
        legacy._clock,
        ids(),
        principal(),
        provider="fake",
        model="scripted",
        import_window=(source.sent_at - timedelta(days=1), source.sent_at + timedelta(days=1)),
        import_guard=guard,
    )
    result = await imported.form(source, [old_fact])
    assert len(result) == 1
    assert guarded
    assert result[0].valid_from == source.sent_at


async def _seed_confirmed_alex(uow: Any, since: Any) -> Person:
    from uuid import uuid4

    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Alex", state="active", **common)
    await uow.people.put(person, expected_revision=0)
    await uow.people.put(
        PersonIdentifier(
            id=uuid4(),
            person_id=person.id,
            identifier_kind="email",
            namespace="owner",
            value="alex@example.test",
            context="owner",
            verification="owner_confirmed",
            valid_from=since,
            **common,
        ),
        expected_revision=0,
    )
    return person


@pytest.mark.parametrize("existing_source", ["none", "source", "partial"])
async def test_same_delivered_email_keeps_one_interaction_with_both_account_sources(
    existing_source: str,
) -> None:
    import json

    from agent_core.domain.email import EmailAccount, EmailRecord
    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.people_sources import email_source_id
    from agent_core.memory.people_correspondence import project_correspondence
    from tests.contract.support import NOW

    factory, _legacy, source, _fact, _ = await semantic_stack(
        message_id_header="<shared@example.test>",
        to="Owner <owner@example.test>",
        label_ids=["INBOX"],
    )
    async with factory() as uow:
        event = (await uow.events.list_after(source.session_id, 0, principal()))[0]
        header = json.loads(event.payload["result_item"]["content"][0]["text"])["messages"][0]
        for name in ["work", "personal"]:
            await uow.email.put(
                EmailRecord(
                    tenant_id=principal().tenant_id,
                    principal_id=principal().principal_id,
                    kind="account",
                    key=name,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                    payload=EmailAccount(
                        id=name, label=name, email_address="owner@example.test", status="ready"
                    ).model_dump(mode="json"),
                ),
                expected_revision=0,
            )
        # Only a known sender has received-mail history (ADR-0121).
        await _seed_confirmed_alex(uow, source.sent_at)
        if existing_source == "source":
            await uow.people.put(
                PeopleSource(
                    id=email_source_id(principal(), source),
                    session_id=source.session_id,
                    event_sequence=source.source_event_sequence,
                    source_kind="email",
                    evidence_at=source.sent_at,
                    account_id=source.account_id,
                    thread_id=source.provider_thread_id,
                    message_id=source.message_id,
                    source_revision="0" * 64,
                    tenant_id=principal().tenant_id,
                    principal_id=principal().principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                    sensitivity=Sensitivity.SENSITIVE,
                ),
                expected_revision=0,
            )
        if existing_source == "partial":
            await project_correspondence(
                uow, principal(), source, {**header, "body_complete": False}, event, NOW
            )
        await project_correspondence(uow, principal(), source, header, event, NOW)
        copied = source.model_copy(update={"account_id": "personal", "message_id": "other-id"})
        await project_correspondence(
            uow, principal(), copied, {**header, "id": "other-id"}, event, NOW
        )
        query = PeopleQuery(
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
            kinds=["interaction"],
            sensitivity_ceiling=Sensitivity.SENSITIVE,
        )
        rows = await uow.people.query(query)
        assert len(rows) == 1 and len(rows[0].support_ids) == 2
        # A later partial read must reuse the already established complete-copy identity.
        await project_correspondence(
            uow, principal(), source, {**header, "body_complete": False}, event, NOW
        )
        assert await uow.people.query(query) == rows
        await uow.people.erase_email_source(
            principal(), "work", source.provider_thread_id, frozenset([source.message_id])
        )
        rows = await uow.people.query(query)
        assert len(rows) == 1 and len(rows[0].support_ids) == 1


@pytest.mark.parametrize(
    "work_status, personal_status",
    [("ready", "ready"), ("ready", "unavailable"), ("syncing", "syncing")],
)
async def test_other_verified_owner_mailbox_is_not_created_as_a_person(
    work_status: str, personal_status: str
) -> None:
    import json

    from agent_core.domain.email import EmailAccount, EmailRecord
    from agent_core.memory.people_correspondence import project_correspondence
    from tests.contract.support import NOW

    message: dict[str, Any] = {
        "from": "Owner <personal@example.test>",
        "to": "Alex <alex@example.test>",
        "label_ids": ["SENT"],
    }
    factory, _, source, _, _ = await semantic_stack(**message)
    async with factory() as uow:
        event = (await uow.events.list_after(source.session_id, 0, principal()))[0]
        header = json.loads(event.payload["result_item"]["content"][0]["text"])["messages"][0]
        for name, address, status in [
            ("work", "work@example.test", work_status),
            ("personal", "personal@example.test", personal_status),
        ]:
            await uow.email.put(
                EmailRecord(
                    tenant_id=principal().tenant_id,
                    principal_id=principal().principal_id,
                    kind="account",
                    key=name,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                    payload=EmailAccount(
                        id=name,
                        label=name,
                        email_address=address,
                        status=status,  # type: ignore[arg-type]
                    ).model_dump(mode="json"),
                ),
                expected_revision=0,
            )
        await project_correspondence(uow, principal(), source, header, event, NOW)
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kinds=["person", "interaction"],
                sensitivity_ceiling=Sensitivity.SENSITIVE,
            )
        )
        assert [row.display_name for row in rows if isinstance(row, Person)] == ["Alex"]
        from agent_core.domain.people import PeopleInteraction

        history = [row for row in rows if isinstance(row, PeopleInteraction)]
        assert len(history) == 1 and history[0].direction == "outgoing"


# ---------------------------------------------------------------------------
# ADR-0121: mail never adds a person from a name in its body.
# ---------------------------------------------------------------------------


async def test_email_body_mentions_form_unlinked_facts_and_create_no_people() -> None:
    from agent_core.domain.people import PersonMention

    factory, legacy, source, _fact, _ = await semantic_stack(
        age=3, body="Alex is Maya's colleague."
    )
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    [belief] = await service.form(source, [colleague_fact(source)])
    assert belief.subject.startswith("person:unresolved:")
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kinds=["person", "relationship", "memory_link", "mention"],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
            )
        )
    assert not [row for row in rows if not isinstance(row, PersonMention)]
    assert rows and all(isinstance(row, PersonMention) and row.person_id is None for row in rows)


async def test_email_body_mention_links_an_existing_person_in_sender_context() -> None:
    import hashlib

    factory, legacy, source, _fact, _ = await semantic_stack(
        age=3, body="Alex is Maya's colleague."
    )
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    maya = Person(id=uuid4(), display_name="Maya", state="active", **common)
    async with factory() as uow:
        await uow.people.put(maya, expected_revision=0)
        await uow.people.put(
            PersonIdentifier(
                id=uuid4(),
                person_id=maya.id,
                identifier_kind="name",
                namespace="owner",
                value="Maya",
                context="email:" + hashlib.sha256(source.sender.encode()).hexdigest(),
                verification="contextual",
                valid_from=NOW - timedelta(days=30),
                **common,
            ),
            expected_revision=0,
        )
    [belief] = await service.form(source, [colleague_fact(source)])
    async with factory() as uow:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                kinds=["person", "memory_link"],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
            )
        )
    assert [row.id for row in rows if isinstance(row, Person)] == [maya.id]
    links = [row for row in rows if isinstance(row, PersonMemoryLink)]
    assert [(link.person_id, link.belief_id, link.unresolved) for link in links] == [
        (maya.id, belief.id, False)
    ]


async def test_unsupported_email_people_label_keeps_the_passage_facts() -> None:
    """A People label the passage does not support drops the link, not the fact."""
    factory, legacy, source, _fact, _ = await semantic_stack(
        age=3, body="Alex is Maya's colleague."
    )
    service = EmailPeopleFormationService(
        factory, legacy._clock, ids(), principal(), provider="fake", model="scripted"
    )
    [belief] = await service.form(source, [colleague_fact(source, maya_label="Maya Brook")])
    assert belief.consolidation_policy_version == "email-semantic@2"


# ---------------------------------------------------------------------------
# ADR-0121: correspondence records the people the owner writes to.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["syncing", "unavailable"])
async def test_correspondence_projects_regardless_of_account_status(status: str) -> None:
    from agent_core.domain.people import PeopleInteraction

    factory, service = await correspondence_stack()
    await seed_account(factory, status=status)
    await service.register_source(
        await mail(factory, message_id="m-sent", sender=OWNER, to=ALEX, labels=["SENT"])
    )
    async with factory() as uow:
        people = [r for r in await uow.people.query(all_rows(["person"])) if isinstance(r, Person)]
        assert [person.display_name for person in people] == ["Alex Rivera"]
        [history] = await uow.people.query(all_rows(["interaction"]))
    assert isinstance(history, PeopleInteraction) and history.direction == "outgoing"


async def test_unknown_sender_gets_an_unattached_endpoint_but_no_person_or_history() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory)
    await service.register_source(await mail(factory, message_id="m-in", sender=ALEX))
    async with factory() as uow:
        assert await uow.people.query(all_rows(["person", "interaction"])) == []
        identifiers = await uow.people.query(all_rows(["identifier"]))
    assert [(i.value, i.person_id) for i in identifiers if isinstance(i, PersonIdentifier)] == [
        ("alex@example.test", None)
    ]


async def test_first_reply_creates_the_person_and_adopts_earlier_received_mail() -> None:
    from agent_core.domain.people import PeopleInteraction

    factory, service = await correspondence_stack()
    await seed_account(factory)
    for index, days in enumerate((5, 4)):
        await service.register_source(
            await mail(factory, message_id=f"m-in-{index}", sender=ALEX, days_ago=days)
        )
    reply = await mail(
        factory, message_id="m-reply", sender=OWNER, to=ALEX, labels=["SENT"], days_ago=3
    )
    await service.register_source(reply)
    await service.register_source(reply)
    async with factory() as uow:
        people = [r for r in await uow.people.query(all_rows(["person"])) if isinstance(r, Person)]
        assert [person.display_name for person in people] == ["Alex Rivera"]
        history = [
            r
            for r in await uow.people.query(all_rows(["interaction"]))
            if isinstance(r, PeopleInteraction)
        ]
        identifiers = [
            r
            for r in await uow.people.query(all_rows(["identifier"]))
            if isinstance(r, PersonIdentifier)
        ]
    assert sorted(row.direction for row in history) == ["incoming", "incoming", "outgoing"]
    assert all(p.person_id == people[0].id for row in history for p in row.participants)
    assert all(row.person_id == people[0].id for row in identifiers)


async def test_out_of_order_mail_attaches_to_the_existing_person_without_duplicates() -> None:
    from agent_core.domain.people import PeopleInteraction

    factory, service = await correspondence_stack()
    await seed_account(factory)
    # The refresh walks history newest first: the reply arrives before older mail.
    await service.register_source(
        await mail(
            factory, message_id="m-reply", sender=OWNER, to=ALEX, labels=["SENT"], days_ago=1
        )
    )
    await service.register_source(await mail(factory, message_id="m-in", sender=ALEX, days_ago=4))
    await service.register_source(
        await mail(
            factory, message_id="m-old-sent", sender=OWNER, to=ALEX, labels=["SENT"], days_ago=6
        )
    )
    async with factory() as uow:
        people = [r for r in await uow.people.query(all_rows(["person"])) if isinstance(r, Person)]
        history = [
            r
            for r in await uow.people.query(all_rows(["interaction"]))
            if isinstance(r, PeopleInteraction)
        ]
    assert [person.display_name for person in people] == ["Alex Rivera"]
    assert sorted(row.direction for row in history) == ["incoming", "outgoing", "outgoing"]


async def _seed_holders(factory: Any, *holders: tuple[str, float, float | None]) -> list[Person]:
    """Owner-confirmed holders of alex@example.test: (name, from days ago, to days ago)."""
    common: PeopleFields = {
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    people = []
    async with factory() as uow:
        for name, start, end in holders:
            person = Person(id=uuid4(), display_name=name, state="active", **common)
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PersonIdentifier(
                    id=uuid4(),
                    person_id=person.id,
                    identifier_kind="email",
                    namespace="owner",
                    value="alex@example.test",
                    context="owner",
                    verification="owner_confirmed",
                    valid_from=NOW - timedelta(days=start),
                    valid_to=None if end is None else NOW - timedelta(days=end),
                    **common,
                ),
                expected_revision=0,
            )
            people.append(person)
    return people


async def test_ended_owner_assignment_blocks_backdated_attachment() -> None:
    """A reassigned address never pulls older mail onto its current holder."""
    from agent_core.domain.people import PeopleInteraction

    factory, service = await correspondence_stack()
    await seed_account(factory, status="ready")
    _former, current = await _seed_holders(
        factory, ("Former holder", 20, 8), ("Alex Rivera", 2, None)
    )
    await service.register_source(await mail(factory, message_id="m-gap", sender=ALEX, days_ago=5))
    async with factory() as uow:
        assert await uow.people.query(all_rows(["interaction"])) == []
    # Later mail from the current holder records its own history and adopts nothing.
    await service.register_source(await mail(factory, message_id="m-now", sender=ALEX, days_ago=1))
    async with factory() as uow:
        history = [
            r
            for r in await uow.people.query(all_rows(["interaction"]))
            if isinstance(r, PeopleInteraction)
        ]
        gap = [
            r
            for r in await uow.people.query(all_rows(["identifier"]))
            if isinstance(r, PersonIdentifier)
            and r.verification == "channel_observed"
            and r.person_id is None
        ]
    assert [[p.person_id for p in row.participants] for row in history] == [[current.id]]
    assert len(gap) == 1


async def test_mail_the_owner_sent_during_an_assignment_gap_creates_no_duplicate() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory, status="ready")
    await _seed_holders(factory, ("Former holder", 20, 8), ("Alex Rivera", 2, None))
    await service.register_source(
        await mail(factory, message_id="m-gap", sender=OWNER, to=ALEX, labels=["SENT"], days_ago=5)
    )
    async with factory() as uow:
        people = [r for r in await uow.people.query(all_rows(["person"])) if isinstance(r, Person)]
        assert sorted(person.display_name for person in people) == ["Alex Rivera", "Former holder"]
        assert await uow.people.query(all_rows(["interaction"])) == []


async def test_writing_to_a_released_address_adds_its_new_holder() -> None:
    """Ending an assignment frees the address for whoever the owner writes to next."""
    factory, service = await correspondence_stack()
    await seed_account(factory, status="ready")
    await _seed_holders(factory, ("Former holder", 20, 8))
    await service.register_source(
        await mail(
            factory,
            message_id="m-new",
            sender=OWNER,
            to="Sam Lee <alex@example.test>",
            labels=["SENT"],
            days_ago=1,
        )
    )
    async with factory() as uow:
        people = [r for r in await uow.people.query(all_rows(["person"])) if isinstance(r, Person)]
    assert sorted(person.display_name for person in people) == ["Former holder", "Sam Lee"]


async def test_late_copy_of_older_mail_keeps_an_ended_assignment_ended() -> None:
    from agent_core.memory.people import resolve_identity

    factory, service = await correspondence_stack()
    await seed_account(factory, status="ready")
    [former] = await _seed_holders(factory, ("Former holder", 20, 2))
    await service.register_source(await mail(factory, message_id="m-old", sender=ALEX, days_ago=5))
    async with factory() as uow:
        copies = [
            r
            for r in await uow.people.query(all_rows(["identifier"]))
            if isinstance(r, PersonIdentifier) and r.verification == "channel_observed"
        ]
        resolved = await resolve_identity(
            uow.people,
            principal(),
            kind="email",
            namespace="owner",
            value="alex@example.test",
            context="owner",
            at=NOW,
            ceiling=Sensitivity.SENSITIVE,
        )
    assert [(row.person_id, row.valid_to) for row in copies] == [
        (former.id, NOW - timedelta(days=2))
    ]
    assert resolved.status == "unresolved"


async def test_heavy_correspondent_keeps_matching_after_many_messages() -> None:
    from agent_core.domain.people import PeopleInteraction

    factory, service = await correspondence_stack()
    await seed_account(factory)
    await service.register_source(
        await mail(
            factory, message_id="m-first-reply", sender=OWNER, to=ALEX, labels=["SENT"], days_ago=20
        )
    )
    for index in range(105):
        await service.register_source(
            await mail(factory, message_id=f"m-{index}", sender=ALEX, days_ago=10 - index / 20)
        )
    async with factory() as uow:
        people = [r for r in await uow.people.query(all_rows(["person"])) if isinstance(r, Person)]
        assert [person.display_name for person in people] == ["Alex Rivera"]
        history: list[PeopleInteraction] = []
        after = None
        while True:
            page = await uow.people.query(
                all_rows(["interaction"]).model_copy(update={"after": after})
            )
            history.extend(row for row in page[:100] if isinstance(row, PeopleInteraction))
            if len(page) <= 100:
                break
            after = page[99].id
    assert len(history) == 106


async def test_role_nameless_and_owner_named_recipients_create_no_person() -> None:
    factory, service = await correspondence_stack()
    await seed_account(factory, status="ready")
    await service.register_source(
        await mail(
            factory,
            message_id="m-sent",
            sender=OWNER,
            to=(
                "Support <support@vendor.test>, bob@example.test, "
                "Owner Name <owner.other@example.test>"
            ),
            labels=["SENT"],
        )
    )
    async with factory() as uow:
        assert await uow.people.query(all_rows(["person", "identifier", "interaction"])) == []


async def test_erased_correspondence_ids_skip_without_aborting_registration() -> None:
    from agent_core.domain.email_semantics import semantic_source_key

    factory, service = await correspondence_stack()
    await seed_account(factory, status="ready")
    reply = await mail(factory, message_id="m-reply", sender=OWNER, to=ALEX, labels=["SENT"])
    await service.register_source(reply)
    async with factory() as uow:
        erased = [row.id for row in await uow.people.query(all_rows(["interaction"]))]
        await uow.people.erase(principal(), erased, preserve_independent=True)
    later = await mail(factory, message_id="m-reply-copy", sender=OWNER, to=ALEX, labels=["SENT"])
    await service.register_source(reply)
    await service.register_source(later)
    async with factory() as uow:
        key = semantic_source_key("work", later.provider_thread_id, later.message_id)
        assert await uow.email.get(principal(), "semantic_source", key) is not None


async def test_group_service_and_address_named_recipients_create_no_person() -> None:
    """A team, a service, or an address shown as a name is not someone you know (ADR-0125)."""
    factory, service = await correspondence_stack()
    await seed_account(factory, status="ready")
    await service.register_source(
        await mail(
            factory,
            message_id="m-sent",
            sender=OWNER,
            to=(
                "Investment Team <invest@fund.test>, Partners <deals@fund.test>, "
                "API OAuth Dev Verification <api-oauth-dev-verification@google.test>, "
                '"iron@gracepres.test" <iron@gracepres.test>, Dana Reyes <dana@fund.test>'
            ),
            labels=["SENT"],
        )
    )
    async with factory() as uow:
        people = [r for r in await uow.people.query(all_rows(["person"])) if isinstance(r, Person)]
    assert [person.display_name for person in people] == ["Dana Reyes"]
