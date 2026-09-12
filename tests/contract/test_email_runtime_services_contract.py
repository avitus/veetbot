"""Projection adapters preserve source CAS and typed task ownership."""

from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest

from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.ports.email import EmailRuntimeServices
from tests.gates.test_email_experience_m26 import email_client
from tests.gates.test_email_learning_m26 import observation, prepare


async def assert_email_runtime_services_contract(
    service: EmailRuntimeServices, principal: Principal, normalized: dict[str, Any]
) -> None:
    source_session = uuid4()
    first = await service.import_thread(principal, "work", normalized, source_session)
    repeated = await service.import_thread(principal, "work", normalized, source_session)
    assert repeated.id == first.id and repeated.revision == first.revision
    assert await service.get_task(principal, uuid4()) is None
    changed = deepcopy(normalized)
    changed["messages"][0]["body"] += " The agenda has changed."
    latest = await service.import_thread(principal, "work", changed, source_session)
    assert latest.id == first.id and latest.revision == first.revision + 1
    with pytest.raises(ConflictError):
        await service.save_assessment(principal, first.id, first.revision, {})
    with pytest.raises(ConflictError):
        await service.save_generated_draft(
            principal, first.id, first.revision, "An obsolete reply.", run_id=uuid4()
        )
    profile = await service.learning_context(principal, latest)
    assessed = await service.save_assessment(
        principal,
        latest.id,
        latest.revision,
        {
            "profile_revision": profile["profile_revision"],
            "content_importance": 1,
            "relationship_importance": 0,
            "urgency": 0,
            "bulk": False,
            "needs_reply": True,
            "summary": "Agenda update",
            "reason": "Updated request",
        },
    )
    assert assessed.revision == latest.revision and assessed.needs_reply
    draft = await service.save_generated_draft(
        principal,
        latest.id,
        latest.revision,
        "Thanks. I will review the updated agenda.",
        run_id=uuid4(),
    )
    assert draft.thread_id == first.id and draft.source_revision == latest.revision
    assert draft.account_id == first.account_id
    assert draft.to == ["reply@example.com"]
    assert draft.in_reply_to == "<m1@example.com>"


async def test_shared_email_projection_service_contract() -> None:
    async with email_client() as (composition, _):
        service: EmailRuntimeServices = await prepare(composition)
        await assert_email_runtime_services_contract(service, composition.principal, observation())
