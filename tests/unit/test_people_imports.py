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
