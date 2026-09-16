"""Tool output is bounded public memory, never persistence metadata."""

from dataclasses import replace
from uuid import uuid4

import jsonschema
import pytest

from agent_core.application.people import PublicPeopleService
from agent_core.domain.agents import Principal
from agent_core.domain.people import InteractionParticipant, PeopleInteraction, Person
from agent_core.domain.policies import TrustLevel
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.tools.people import PeopleContextTool, PeopleHistoryTool, PeopleSearchTool
from tests.contract.people_fixtures import PeopleFields
from tests.contract.support import NOW, memory_uow_factory, principal, run, tool_context


@pytest.mark.parametrize("operation", ["search", "history"])
async def test_people_tools_register_influence_before_returning_to_the_runtime(
    operation: str,
) -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read", "people.write"}})
    await tool_read_erasure_contract(factory, clock, owner, operation=operation)


async def tool_read_erasure_contract(
    factory: UnitOfWorkFactory, clock: Clock, owner: Principal, *, operation: str
) -> None:
    from agent_core.application.people_erasure import PeopleErasureService
    from agent_core.domain.memory import Sensitivity
    from agent_core.domain.people_views import PeopleForgetRequest
    from agent_core.domain.runs import RunStatus

    context = replace(tool_context(), principal=owner)
    person = Person(
        id=uuid4(),
        display_name="Maya",
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        created_at=NOW,
        updated_at=NOW,
    )
    async with factory() as uow:
        await uow.people.put(person, expected_revision=0)
        await uow.runs.create(run(status=RunStatus.RUNNING))
    if operation == "search":
        result = await PeopleSearchTool(factory, clock).execute({"text": "Maya"}, context)
        assert result.structured and result.structured["candidates"]
    else:
        result = await PeopleHistoryTool(PublicPeopleService(factory, clock)).execute(
            {"person_id": str(person.id)}, context
        )
    assert result.ok
    # No tool invocation result has been persisted yet: erasure must find the
    # read itself, rather than depend on the runtime saving its eventual output.
    erasure = PeopleErasureService(factory, clock)
    preview = await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(session_id=context.session_id, phase="preview", expected_revision=1),
        key="tool-forget-preview",
        ceiling=Sensitivity.SENSITIVE,
    )
    await erasure.forget(
        owner,
        person.id,
        PeopleForgetRequest(
            session_id=context.session_id,
            phase="apply",
            operation_id=preview.id,
            expected_revision=preview.revision,
        ),
        key="tool-forget-apply",
        ceiling=Sensitivity.SENSITIVE,
    )
    async with factory() as uow:
        assert (await uow.runs.get(context.run_id, owner)).cancel_requested_at is not None


async def test_remember_without_people_preserves_the_advertised_ordinary_default() -> None:
    from agent_core.domain.memory import Sensitivity
    from agent_core.memory.formation import GovernedMemoryService
    from agent_core.tools.memory_remember import PeopleMemoryRememberTool
    from tests.contract.memory_fixtures import user_event
    from tests.contract.support import ids

    clock, factory = await memory_uow_factory()
    await user_event(factory, "Remember that I prefer jasmine tea.")
    service = GovernedMemoryService(factory, clock, ids(), principal(), people_enabled=True)
    tool = PeopleMemoryRememberTool(service)
    assert tool.spec.input_schema["properties"]["sensitivity"]["default"] == "internal"
    result = await tool.execute(
        {"statement": "I prefer jasmine tea.", "subject": "tea", "scope": "user"}, tool_context()
    )
    assert result.ok
    assert (await service.list_memories())[0].sensitivity is Sensitivity.INTERNAL


async def test_history_tool_has_closed_public_output_and_shared_token_bound() -> None:
    clock, factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"people.read"}})
    common: PeopleFields = {
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    person = Person(id=uuid4(), display_name="Maya", **common)
    async with factory() as uow:
        await uow.runs.create(run())
        await uow.people.put(person, expected_revision=0)
        for _ in range(6):
            await uow.people.put(
                PeopleInteraction(
                    id=uuid4(),
                    channel="chat",
                    interaction_kind="meeting",
                    attribution="owner_reported",
                    direction="reported",
                    summary="Discussed the upcoming visit. " * 30,
                    participants=[InteractionParticipant(person_id=person.id, role="participant")],
                    **common,
                ),
                expected_revision=0,
            )
    tool = PeopleHistoryTool(PublicPeopleService(factory, clock))
    context = replace(tool_context(), principal=owner)
    result = await tool.execute({"person_id": str(person.id)}, context)
    assert result.output_trust is TrustLevel.MEMORY
    assert "tenant_id" not in result.model_dump_json()
    assert "principal_id" not in result.model_dump_json()
    assert hasattr(result.content[0], "text") and result.structured is not None
    assert len(result.content[0].text.encode()) <= 8000
    assert result.structured["next_cursor"] is not None
    for tool_class in (PeopleSearchTool, PeopleContextTool, PeopleHistoryTool):
        assert tool_class.spec.output_schema is not None
        assert tool_class.spec.output_schema.get("additionalProperties") is False
    assert tool.spec.output_schema is not None
    jsonschema.validate(result.structured, tool.spec.output_schema)
    second = await tool.execute(
        {"person_id": str(person.id), "cursor": result.structured["next_cursor"]}, context
    )
    assert second.structured is not None
    assert not (
        {item["id"] for item in result.structured["items"]}
        & {item["id"] for item in second.structured["items"]}
    )
