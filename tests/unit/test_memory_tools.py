"""Static coverage for the agent-facing memory tool surface.

memory.search and memory.recall_episodes previously had only Postgres
golden-journey coverage; these tests pin their contract statically: hard
principal scoping from the execution context, TrustLevel.MEMORY labeling on
everything returned, and the structured outputs the model consumes.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from agent_core.domain.memory import BeliefType, MemoryAuthority
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, TrustLevel
from agent_core.memory.retrieval import EventEpisodeSearch
from agent_core.tools.memory_recall_episodes import MemoryRecallEpisodesTool
from agent_core.tools.memory_remember import MemoryRememberTool
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.memory_fixtures import formation_stack, memory, session_events, user_event
from tests.contract.support import SESSION_ID, principal, session, tool_context


async def test_composition_retains_pinned_legacy_memory_search() -> None:
    from agent_core.bootstrap import build
    from tests.gates.test_runtime_tool_budget import _settings

    async with build(settings=_settings(), principal=principal()) as app:
        registry = app.tool_pipeline._registry
        legacy = registry.get("memory.search", "1.1.0")
        assert legacy.spec.input_schema["required"] == ["text", "scope"]
        assert "belief_types" in legacy.spec.input_schema["properties"]
        assert registry.get("memory.search").spec.version == "2.1.0"
        original = memory()
        async with app.uow_factory() as uow:
            await uow.sessions.create(session())
            await uow.memories.upsert_belief(original)
        arguments = {"text": original.statement, "scope": original.scope}
        found = await legacy.execute(arguments, tool_context())
        filtered = await legacy.execute(
            {**arguments, "belief_types": [BeliefType.FACT.value]}, tool_context()
        )
        assert found.structured is not None
        assert str(original.id) in found.structured["belief_ids"]
        assert filtered.structured is not None
        assert filtered.structured["belief_ids"] == []


async def test_planned_search_uses_the_shared_people_context() -> None:
    from uuid import uuid4

    from agent_core.bootstrap import build
    from agent_core.domain.people import Person
    from tests.contract.support import NOW, agent, session
    from tests.gates.test_runtime_tool_budget import _settings

    owner = principal().model_copy(update={"scopes": {"people.read"}})
    async with build(settings=_settings(), principal=owner) as app:
        async with app.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.sessions.create(session())
            await uow.people.put(
                Person(
                    id=uuid4(),
                    display_name="Quinn",
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision=0,
            )
        search = app.tool_pipeline._registry.get("memory.search")
        result = await search.execute(
            {"text": "Prepare for Quinn", "scope": "project-a"},
            replace(tool_context(), principal=owner),
        )
        assert result.ok and result.memory_recall is not None
        assert result.memory_recall.items == 1
        assert any(
            isinstance(part, TextPart) and "Person: Quinn" in part.text for part in result.content
        )
        assert await app.memory_retriever.validate_retained(
            result.memory_recall, session_id=SESSION_ID, run_id=tool_context().run_id
        ), "Fresh People evidence must survive into the answer request"


async def test_live_search_does_not_ask_the_model_to_guess_hard_storage_filters() -> None:
    from agent_core.bootstrap import build
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from tests.gates.test_runtime_tool_budget import _settings

    async with build(
        settings=_settings(),
        principal=principal(),
        enabled_tools=["memory.search"],
        script=FakeModelScript(turns=[ScriptedTurn(text="Done")]),
    ) as app:
        run_id = await app.runs.submit("Find the remembered project decision")
        await app.runs.wait_terminal(run_id)
        requests = app.executor._model_provider.requests  # type: ignore[attr-defined]
        searches = [tool for tool in requests[-1].tools if tool.name == "memory.search"]
        assert len(searches) == 1
        search = searches[0]
    assert "belief_types" not in search.input_schema["properties"], (
        "A guessed storage kind must not exclude a differently classified true fact"
    )
    assert "type_hints" in search.input_schema["properties"]
    assert search.version == "2.1.0"
    assert search.input_schema["required"] == ["text"]


async def test_answer_request_keeps_fresh_people_search_evidence() -> None:
    from uuid import uuid4

    from agent_core.adapters.determinism import FixedClock
    from agent_core.bootstrap import build
    from agent_core.domain.messages import (
        FakeModelScript,
        ScriptedToolCall,
        ScriptedTurn,
        StopReason,
        ToolResultItem,
    )
    from agent_core.domain.people import Person
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import NOW
    from tests.gates.test_runtime_tool_budget import _requests, _settings

    owner = principal().model_copy(update={"scopes": {"people.read"}})
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search", call_id="person-search", arguments={"text": "Quinn"}
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Prepared from the retrieved evidence."),
        ]
    )
    async with build(
        settings=_settings(),
        principal=owner,
        clock=FixedClock(NOW),
        script=script,
        enabled_tools=["memory.search"],
    ) as app:
        async with app.uow_factory() as uow:
            await uow.people.put(
                Person(
                    id=uuid4(),
                    display_name="Quinn",
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                expected_revision=0,
            )
        run_id = await app.runs.submit("Prepare for my appointment.")
        run = await app.runs.wait_terminal(run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        requests = _requests(app)
        assert len(requests) == 2
        saved = [
            item
            for item in requests[-1].conversation
            if isinstance(item, ToolResultItem) and item.call_id == "person-search"
        ]
        assert len(saved) == 1 and not saved[0].is_error
        assert any(
            isinstance(part, TextPart) and "Person: Quinn" in part.text for part in saved[0].content
        ), "The answer model must see the successful search's current People evidence"


@pytest.mark.parametrize("scope", ["general", "research-project"])
async def test_planned_search_defaults_to_the_owned_session_scope(scope: str) -> None:
    from uuid import UUID

    from agent_core.domain.sessions import project_scope

    _, factory, _, retriever = await formation_stack()
    async with factory() as uow:
        session = await uow.sessions.get(SESSION_ID, principal())
        session = session.model_copy(
            update={"id": UUID(int=867), "metadata": {"project_scope": scope}}
        )
        await uow.sessions.create(session)
    tool = MemorySearchTool(retriever, model_planning=True)
    args = {"text": "remembered project constraints"}
    assert Draft202012Validator(tool.spec.input_schema).is_valid(args)
    tool = MemorySearchTool(retriever, model_planning=True, factory=factory)
    result = await tool.execute(args, replace(tool_context(), session_id=session.id))
    assert result.structured is not None

    async with factory() as uow:
        trace = await uow.traces.get(UUID(result.structured["trace_id"]), principal())
    assert trace.query.current_scope == project_scope(session.metadata) == scope


async def test_omitted_planned_scope_does_not_guess_without_session_binding() -> None:
    _, _, _, retriever = await formation_stack()
    tool = MemorySearchTool(retriever, model_planning=True)
    with pytest.raises(ValueError, match="session scope"):
        await tool.execute({"text": "remembered project constraints"}, tool_context())


@pytest.mark.parametrize("kind", list(BeliefType))
async def test_wrong_planning_type_hint_cannot_hide_a_relevant_original(kind: BeliefType) -> None:
    _, factory, _, retriever = await formation_stack()
    original = memory(statement="Copper sentinel is the deployment approval phrase.").model_copy(
        update={"belief_type": kind}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(original)
    wrong = BeliefType.PREFERENCE if kind is BeliefType.FACT else BeliefType.FACT
    arguments = {"text": "copper sentinel deployment approval", "scope": "project-a"}
    planned = MemorySearchTool(retriever, model_planning=True)
    result = await planned.execute({**arguments, "type_hints": [wrong.value]}, tool_context())
    assert result.structured is not None
    assert str(original.id) in result.structured["belief_ids"]
    # Explicit non-planner filtering retains its original meaning.
    legacy = await MemorySearchTool(retriever).execute(
        {**arguments, "belief_types": [wrong.value]}, tool_context()
    )
    assert legacy.structured is not None and legacy.structured["belief_ids"] == []


async def test_planner_rejects_unknown_hints_and_old_filters_instead_of_widening_them() -> None:
    _, _, _, retriever = await formation_stack()
    tool = MemorySearchTool(retriever, model_planning=True)
    validate = Draft202012Validator(tool.spec.input_schema)
    arguments = {"text": "deployment approval", "scope": "project-a"}
    assert not validate.is_valid({**arguments, "type_hints": ["unknown-kind"]})
    assert not validate.is_valid({**arguments, "belief_types": ["fact"]})
    with pytest.raises(ValueError, match="not hard belief_types"):
        tool.query(
            {**arguments, "belief_types": ["fact"]},
            tenant_id=principal().tenant_id,
            principal_id=principal().principal_id,
        )


async def test_memory_search_tool_labels_results_as_memory_trust() -> None:
    _clock, factory, service, retriever = await formation_stack()
    sequence = await user_event(factory, "Remember that answers should be concise.")
    belief = await service.remember(
        session_id=SESSION_ID,
        run_id=None,
        statement="User prefers concise answers",
        subject="answer style",
        scope="project-a",
        belief_type=BeliefType.PREFERENCE,
        source_event_ids=[sequence],
    )

    result = await MemorySearchTool(retriever).execute(
        {"text": "concise answers", "scope": "project-a"}, tool_context()
    )

    assert result.ok is True
    assert result.output_trust is TrustLevel.MEMORY
    assert MemorySearchTool.spec.output_trust is TrustLevel.MEMORY
    assert MemorySearchTool.spec.idempotency is IdempotencyClass.READ_ONLY
    assert result.structured is not None
    assert result.structured["belief_ids"] == [str(belief.id)]
    assert result.structured["truncated"] is False
    content = result.content[0]
    assert isinstance(content, TextPart)
    assert f"[m:{str(belief.id)[:8]}]" in content.text


async def test_memory_search_tool_cannot_reach_other_principals() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    foreign = memory().model_copy(update={"principal_id": "principal-b"})
    async with factory() as uow:
        await uow.memories.upsert_belief(foreign)

    result = await MemorySearchTool(retriever).execute(
        {"text": "concise answers", "scope": "project-a"}, tool_context()
    )

    assert result.ok is True
    assert result.structured is not None
    assert result.structured["belief_ids"] == []


async def test_default_scope_preserves_local_evidence_and_explicit_scope_restrictions() -> None:
    from agent_core.domain.memory import Portability
    from agent_core.domain.sessions import project_scope

    _, factory, _, retriever = await formation_stack()
    async with factory() as uow:
        session = await uow.sessions.get(SESSION_ID, principal())
        scope = project_scope(session.metadata)
        belief = memory(statement="Copper sentinel is the deployment approval phrase.").model_copy(
            update={"scope": scope, "origin_scopes": [scope], "portability": Portability.LOCAL}
        )
        await uow.memories.upsert_belief(belief)
    tool = MemorySearchTool(retriever, model_planning=True, factory=factory)
    arguments = {"text": "copper sentinel deployment approval"}
    here = await tool.execute(arguments, tool_context())
    elsewhere = await tool.execute({**arguments, "scope": "another-project"}, tool_context())
    assert here.structured is not None and str(belief.id) in here.structured["belief_ids"]
    assert elsewhere.structured is not None and elsewhere.structured["belief_ids"] == []


async def test_default_scope_requires_an_accessible_session_and_never_guesses() -> None:
    from agent_core.domain.errors import NotFoundError

    _, factory, _, retriever = await formation_stack()
    arguments = {"text": "Project constraints"}
    with pytest.raises(ValueError, match="trusted session scope"):
        await MemorySearchTool(retriever, model_planning=True).execute(arguments, tool_context())
    foreign = principal().model_copy(update={"principal_id": "someone-else"})
    tool = MemorySearchTool(retriever, model_planning=True, factory=factory)
    with pytest.raises(NotFoundError):
        await tool.execute(arguments, replace(tool_context(), principal=foreign))


async def test_recall_episodes_tool_returns_trusted_sorted_events() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    first = await user_event(factory, "alpha deploy discussion")
    second = await user_event(factory, "beta rollback note")

    tool = MemoryRecallEpisodesTool(EventEpisodeSearch(factory, principal()))
    result = await tool.execute({}, tool_context())

    assert result.ok is True
    assert result.output_trust is TrustLevel.MEMORY
    assert result.structured is not None
    events = result.structured["events"]
    assert isinstance(events, list)
    assert [item["sequence"] for item in events] == [first, second]
    assert {item["event_type"] for item in events} == {"user.message.created"}
    content = result.content[0]
    assert isinstance(content, TextPart)
    assert json.loads(content.text) == events


async def test_recall_episodes_tool_filters_by_text() -> None:
    _clock, factory, _service, _retriever = await formation_stack()
    await user_event(factory, "alpha deploy discussion")
    match = await user_event(factory, "beta rollback note")

    tool = MemoryRecallEpisodesTool(EventEpisodeSearch(factory, principal()))
    result = await tool.execute({"text": "rollback"}, tool_context())

    assert result.structured is not None
    assert [item["sequence"] for item in result.structured["events"]] == [match]


async def test_remember_tool_accepts_verbatim_user_quotes_from_untrusted_turns() -> None:
    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "The deployment region is eu-west-1, remember it.")

    result = await MemoryRememberTool(service).execute(
        {
            "statement": "Deployment region is eu-west-1.",
            "subject": "deployment region",
            "scope": "project-a",
        },
        replace(
            tool_context(),
            origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
            argument_trust={"statement": TrustLevel.USER},
        ),
    )

    assert result.ok is True
    assert [item.statement for item in await service.list_memories()] == [
        "Deployment region is eu-west-1."
    ]


async def test_remember_tool_records_affirmed_authority_for_memory_trust_statements() -> None:
    """The tool's authority follows the trust of the statement it was handed.

    A statement arriving at memory trust is the agent affirming something it
    already holds, which is `AFFIRMED` — below a direct user statement and
    above an extractor inference — and the write is the memory speaking, so
    the formation event's actor is memory rather than the principal.
    """

    _clock, factory, service, _retriever = await formation_stack()
    await user_event(factory, "The deployment region is eu-west-1, remember it.")
    tool = MemoryRememberTool(service)

    affirmed = await tool.execute(
        {
            "statement": "Deployment region is eu-west-1.",
            "subject": "deployment region",
            "scope": "project-a",
        },
        replace(tool_context(), origin_trust=TrustLevel.MEMORY),
    )
    stated = await tool.execute(
        {
            "statement": "Rollbacks require a signed manifest.",
            "subject": "rollback policy",
            "scope": "project-a",
        },
        replace(tool_context(), origin_trust=TrustLevel.USER),
    )

    assert (affirmed.ok, stated.ok) == (True, True)
    by_subject = {belief.subject: belief for belief in await service.list_memories()}
    assert by_subject["deployment region"].authority is MemoryAuthority.AFFIRMED
    assert by_subject["rollback policy"].authority is MemoryAuthority.USER
    formation_actors = [
        (event.actor_type, event.payload["belief"]["subject"])
        for event in await session_events(factory)
        if event.event_type == "memory.formed"
    ]
    assert formation_actors == [
        ("memory", "deployment region"),
        ("principal", "rollback policy"),
    ]


async def test_planned_search_keeps_valid_time_separate_from_knowledge_time() -> None:
    from datetime import UTC, datetime

    _, _, _, retriever = await formation_stack()
    planned = MemorySearchTool(retriever, model_planning=True)
    arguments = {
        "text": "previous breakfast constraints",
        "scope": "user",
        "as_of": "2026-09-01T00:00:00Z",
        "known_at": "2026-10-01T00:00:00Z",
    }
    assert Draft202012Validator(planned.spec.input_schema).is_valid(arguments), (
        "The planner must distinguish when a fact applied from when it was recorded"
    )
    query = planned.query(
        arguments, tenant_id=principal().tenant_id, principal_id=principal().principal_id
    )
    assert query.as_of == datetime(2026, 9, 1, tzinfo=UTC)
    assert query.known_at == datetime(2026, 10, 1, tzinfo=UTC)
    current = planned.query(
        {"text": "breakfast constraints", "scope": "user"},
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
    )
    assert current.as_of is None and current.known_at is None
    assert not Draft202012Validator(MemorySearchTool.spec.input_schema).is_valid(arguments)
