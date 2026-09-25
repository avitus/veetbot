"""Which labels can name a person at all (ADR-0121, ADR-0125)."""

import pytest

from agent_core.domain.people import is_group_or_service_name, is_non_person_reference


@pytest.mark.parametrize(
    "label",
    [
        "Investment Team",
        "Partners",
        "API OAuth Dev Verification",
        "iron@gracepres.test",
        "The Smith Family",
        "Board",
        "Customer Support",
        "Scale Managers",
    ],
)
def test_group_service_and_address_labels_never_name_a_person(label: str) -> None:
    assert is_group_or_service_name(label)


@pytest.mark.parametrize(
    "label",
    ["Dana Reyes", "Norman Mailer", "Partner", "Analyst", "Dev Patel", "My brother", "Kyrri"],
)
def test_people_roles_and_surnames_stay_person_labels(label: str) -> None:
    """Singular roles and surnames that are also words still name one person."""
    assert not is_group_or_service_name(label)
    assert not is_non_person_reference(label)
