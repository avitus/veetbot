"""Calendar precision survives source offsets and rejects invented timezone names."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_core.domain.people import PeopleCommitment, PeopleInteraction, RelationshipAssertion
from tests.contract.support import NOW, principal


@pytest.mark.parametrize("kind", ["relationship", "interaction", "commitment"])
@pytest.mark.parametrize("zone", [None, "Asia/Tokyo", "Not/A_Timezone"])
def test_calendar_date_preserves_its_offset_without_inventing_a_named_zone(
    kind: str, zone: str | None
) -> None:
    owner, source, person = principal(), uuid4(), uuid4()
    data: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
        "support_ids": [source],
        "source_timezone": zone,
    }
    endpoint = {"kind": "person", "id": person}
    date = "2026-08-01T00:00:00+09:00"
    model: type[RelationshipAssertion] | type[PeopleInteraction] | type[PeopleCommitment]
    if kind == "relationship":
        model = RelationshipAssertion
        data.update(
            subject=endpoint,
            object={"kind": "owner"},
            predicate="friend",
            belief_id=uuid4(),
            valid_from=date,
            precision="month",
        )
    elif kind == "interaction":
        model = PeopleInteraction
        data.update(
            channel="chat",
            interaction_kind="meeting",
            attribution="owner_reported",
            direction="reported",
            summary="Met in August",
            occurred_at=date,
            precision="month",
            participants=[{"person_id": person, "role": "participant"}],
        )
    else:
        model = PeopleCommitment
        data.update(
            debtor=endpoint,
            beneficiary={"kind": "owner"},
            state="open",
            belief_id=uuid4(),
            description="Send report in August",
            due_at=date,
            due_precision="month",
            state_source_id=source,
        )
    if zone == "Not/A_Timezone":
        with pytest.raises(ValidationError, match="timezone"):
            model.model_validate(data)
    else:
        value = model.model_validate(data)
        assert value.source_timezone == (zone or "GMT+0900")
        assert model.model_validate_json(value.model_dump_json()) == value
