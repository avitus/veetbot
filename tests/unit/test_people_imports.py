"""Import cost and interruption boundaries never widen source authority."""

from decimal import Decimal

import pytest

from agent_core.domain.messages import (
    ModelPricing,
    ModelRequest,
    ResolvedModel,
)
from agent_core.runtime.people_imports import ImportStoppedError, reservation_cost
from tests.contract.support import NOW


def test_mailbox_import_requires_explicit_account_selection() -> None:
    from datetime import timedelta
    from uuid import UUID

    from pydantic import ValidationError

    from agent_core.domain.people_imports import PeopleImportScope

    scope = {
        "since": NOW - timedelta(days=200),
        "until": NOW,
        "max_records": 20,
        "max_cost_usd": Decimal("1"),
        "email_source": "mailbox",
    }
    imported = PeopleImportScope.model_validate({**scope, "account_ids": ["work"]})
    assert imported.model_dump()["email_source"] == "mailbox"
    with pytest.raises(ValidationError, match="mailbox discovery requires"):
        PeopleImportScope.model_validate({**scope, "session_ids": [UUID(int=1)]})


def test_reservation_includes_worst_case_input_output_and_separate_reasoning() -> None:
    request = ModelRequest(
        model_policy="test", conversation=[], tools=[], maximum_output_tokens=100
    )
    model = ResolvedModel(
        provider="test",
        model="test",
        resolved_at=NOW,
        pricing=ModelPricing(
            input_per_mtok=Decimal(1),
            cache_write_per_mtok=Decimal(2),
            output_per_mtok=Decimal(3),
            reasoning_priced_separately=True,
            reasoning_per_mtok=Decimal(4),
        ),
    )
    assert reservation_cost(request, model) == (
        Decimal(model.limits.context_window_tokens * 2 + 700) / 1_000_000
    )
    with pytest.raises(ImportStoppedError, match="pricing"):
        reservation_cost(request, model.model_copy(update={"pricing": ModelPricing()}))


async def test_scoped_import_survives_new_observed_identifiers() -> None:
    """Only owner-confirmed identities pin a scoped import (ADR-0118).

    Correspondence now records who the owner writes to, one identifier per
    message. Those rows must neither exceed the snapshot bound nor stop an
    import that selects the person by the owner's confirmed identifiers.
    """
    from uuid import uuid4

    from agent_core.application.people_imports import import_identity_revisions
    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.people import Person, PersonIdentifier
    from tests.contract.support import NOW, memory_uow_factory, principal

    _, factory = await memory_uow_factory()
    owner = principal()
    common = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Alex", **common)  # type: ignore[arg-type]
    confirmed = PersonIdentifier(
        id=uuid4(),
        person_id=person.id,
        identifier_kind="email",
        namespace="owner",
        value="alex@example.test",
        context="owner",
        verification="owner_confirmed",
        valid_from=NOW,
        **common,  # type: ignore[arg-type]
    )
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        await uow.people.put(confirmed, expected_revision=0)
        before = await import_identity_revisions(uow, owner, [person.id], Sensitivity.RESTRICTED)
        for _ in range(150):
            await uow.people.put(
                confirmed.model_copy(update={"id": uuid4(), "verification": "channel_observed"}),
                expected_revision=0,
            )
        after = await import_identity_revisions(uow, owner, [person.id], Sensitivity.RESTRICTED)
    assert before == after == {person.id: 1, confirmed.id: 1}
