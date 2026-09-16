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
