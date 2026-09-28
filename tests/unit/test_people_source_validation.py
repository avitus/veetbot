"""People identifiers and provider-local keys cannot cross identity boundaries."""

import pytest
from pydantic import ValidationError

from agent_core.domain.people_extraction import OrganizationEvidence, PersonEvidence
from agent_core.domain.people_sources import identifier_occurs


@pytest.mark.parametrize(
    "text,value,kind,expected",
    [
        ("Call +1 212-555-0100 today", "+1 212", "phone", False),
        ("Call +1 212-555-0100 today", "+12125550100", "phone", True),
        ("Call +1 (212) 555.0100 today", "+1 212-555-0100", "phone", True),
        ("Call +121255501001 today", "+12125550100", "phone", False),
        ("Call 212-555-0100 today", "+12125550100", "phone", False),
        ("Anne-Marie called", "Anne", "name", False),
        ("O'Neil called", "Neil", "name", False),
        ("O\u2019Neil called", "Neil", "name", False),
        ("Anne-Marie called", "Anne-Marie", "name", True),
        ("Anne called", "Anne", "name", True),
        ("joann@example.test", "ann@example.test", "email", False),
        ("Write ann@example.test today", "ann@example.test", "email", True),
        ("@ann-other", "@ann", "handle", False),
    ],
)
def test_identifier_requires_a_complete_source_token(
    text: str, value: str, kind: str, expected: bool
) -> None:
    assert identifier_occurs(text, value, kind) is expected


@pytest.mark.parametrize("organization", [False, True])
def test_provider_evidence_cannot_claim_reserved_owner_key(organization: bool) -> None:
    fields: dict[str, object] = {
        "key": "owner",
        "source_event_id": 1,
        "start": 0,
        "end": 4,
        "text": "Acme",
        "display_name": "Acme",
    }
    if organization:
        with pytest.raises(ValidationError, match="reserved"):
            OrganizationEvidence.model_validate(fields)
    else:
        fields.update(
            identifier_kind="name",
            identifier_value="Acme",
            namespace="owner",
            context="",
            role="subject",
            referent_key=None,
        )
        with pytest.raises(ValidationError, match="reserved"):
            PersonEvidence.model_validate(fields)


def _mention(key: str, *, text: str = "Maya", start: int = 0) -> dict[str, object]:
    return {
        "key": key,
        "source_event_id": 1,
        "start": start,
        "end": start + len(text),
        "text": text,
        "display_name": text,
        "identifier_kind": "name",
        "identifier_value": text,
        "namespace": "owner",
        "context": "",
        "role": "subject",
        "referent_key": None,
    }


def _relationship(subject: str, object_: str) -> dict[str, object]:
    return {
        "subject_key": subject,
        "object_key": object_,
        "predicate": "friend",
        "qualifier": "",
        "valid_from": None,
        "valid_to": None,
        "precision": "unknown",
        "source_timezone": None,
    }


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"end": 5}, "offsets must match its exact text"),
        ({"start": 1}, "offsets must match its exact text"),
    ],
)
def test_person_evidence_offsets_must_span_exactly_its_text(
    fields: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        PersonEvidence.model_validate(_mention("maya") | fields)


@pytest.mark.parametrize(
    "fields",
    [{"end": 5}, {"display_name": "Acme Corp"}],
)
def test_organization_evidence_requires_an_exact_name_span(fields: dict[str, object]) -> None:
    valid: dict[str, object] = {
        "key": "acme",
        "source_event_id": 1,
        "start": 0,
        "end": 4,
        "text": "Acme",
        "display_name": "Acme",
    }
    OrganizationEvidence.model_validate(valid)
    with pytest.raises(ValidationError, match="exact name span"):
        OrganizationEvidence.model_validate(valid | fields)


@pytest.mark.parametrize(
    ("mentions", "relationship", "message"),
    [
        (["maya", "maya"], None, "keys must be unique"),
        (["maya"], ("maya", "sam"), "must name local evidence or the owner"),
        (["maya"], ("owner", "maya"), None),
        (["maya", "sam"], ("sam", "maya"), None),
    ],
)
def test_people_claim_endpoints_name_only_local_evidence_or_the_owner(
    mentions: list[str], relationship: tuple[str, str] | None, message: str | None
) -> None:
    """A provider cannot relate a person it never cited, only one it quoted or the owner."""

    from agent_core.domain.people_extraction import PeopleClaim

    claim = {
        "mentions": [
            _mention(key, text=key.capitalize(), start=index * 10)
            for index, key in enumerate(mentions)
        ],
        "organizations": [],
        "relationship": None if relationship is None else _relationship(*relationship),
        "commitment": None,
    }
    if message is None:
        assert PeopleClaim.model_validate(claim).relationship is not None
    else:
        with pytest.raises(ValidationError, match=message):
            PeopleClaim.model_validate(claim)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"participant_keys": ["sam"]}, "unique local mention evidence"),
        ({"participant_keys": ["maya", "maya"]}, "participant keys must be unique"),
        ({"source_event_id": 2}, "mentions must belong to its source"),
        ({"summary": "Dinner with Maya"}, "exact evidence excerpt"),
    ],
)
def test_interaction_evidence_is_bound_to_its_own_source(
    changes: dict[str, object], message: str
) -> None:
    from agent_core.domain.people_extraction import InteractionEvidence

    valid: dict[str, object] = {
        "source_event_id": 1,
        "text": "Lunch with Maya on Friday",
        "summary": "Lunch with Maya",
        "interaction_kind": "meeting",
        "occurred_at": None,
        "precision": "unknown",
        "source_timezone": None,
        "mentions": [_mention("maya", start=11)],
        "participant_keys": ["maya"],
    }
    assert InteractionEvidence.model_validate(valid).participant_keys == ["maya"]
    with pytest.raises(ValidationError, match=message):
        InteractionEvidence.model_validate(valid | changes)
