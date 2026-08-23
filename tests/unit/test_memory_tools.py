"""Static coverage for the agent-facing memory tool surface.

memory.search and memory.recall_episodes previously had only Postgres
golden-journey coverage; these tests pin their contract statically: hard
principal scoping from the execution context, TrustLevel.MEMORY labeling on
everything returned, and the structured outputs the model consumes.
"""

from __future__ import annotations

import json
from dataclasses import replace

from agent_core.domain.memory import BeliefType, MemoryAuthority
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, TrustLevel
from agent_core.memory.retrieval import EventEpisodeSearch
from agent_core.tools.memory_recall_episodes import MemoryRecallEpisodesTool
from agent_core.tools.memory_remember import MemoryRememberTool
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.memory_fixtures import formation_stack, memory, session_events, user_event
from tests.contract.support import SESSION_ID, principal, tool_context


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
