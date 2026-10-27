"""Context remains source-backed across an ordinary consolidation watermark."""

import json

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.domain.messages import FakeModelScript, ResolvedModel, ScriptedTurn
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.people_context_distillation import (
    ContextualPeopleCandidateExtractor as PeopleAssistedCandidateExtractor,
)
from agent_core.ports.persistence import UnitOfWorkFactory
from tests.contract.memory_fixtures import user_event
from tests.contract.support import SESSION_ID, ids, memory_uow_factory, principal


@pytest.mark.parametrize("history_only", [False, True])
@pytest.mark.parametrize("source_isolated", [False, True])
async def test_prior_owner_context_is_cited_but_never_reextracted(
    history_only: bool, source_isolated: bool
) -> None:
    clock, factory = await memory_uow_factory()
    await context_formation_contract(clock, factory, history_only, source_isolated)


async def context_formation_contract(
    clock: FixedClock,
    factory: UnitOfWorkFactory,
    history_only: bool = False,
    source_isolated: bool = False,
) -> None:
    context = "What do you think of the new Aurora assistant?"
    observation = "Setting a timer now takes almost 18 seconds."
    first = await user_event(factory, context)
    current = await user_event(factory, observation)
    statement = "User reports that Aurora takes almost 18 seconds to set a timer."
    candidate = {
        "subject": "Aurora timer delay",
        "statement": statement,
        "source_event_ids": [first] if history_only else [first, current],
        "sensitivity_guess": "internal",
        "claim_kind": "recurring_state",
        "derivation": "direct",
        "polarity": "assert",
        "people": None,
        "context_anchors": [{"source_event_id": first, "text": "Aurora"}],
        "evidence_spans": [{"source_event_id": first, "text": context}]
        + ([] if history_only else [{"source_event_id": current, "text": observation}]),
    }
    responses = [
        {
            "episodes": [
                {
                    "narrative": f"[e:{current}] {observation}",
                    "subjects": [],
                    "source_event_ids": [current],
                }
            ]
        },
        {"predictions": []},
        {
            "candidates": [candidate],
            "interactions": [],
            "coverage": [
                {
                    "coverage_unit_id": f"{current}:1",
                    "decision": "formed",
                    "candidate_indexes": [0],
                    "prediction_indexes": [],
                }
            ],
        },
    ]
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text=json.dumps(reply)) for reply in responses]), clock
    )
    extractor = PeopleAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(provider="fake", model="scripted", resolved_at=clock.now()),
        uow_factory=factory,
        clock=clock,
        ids=ids(),
        source_isolated=source_isolated,
    )
    service = GovernedMemoryService(
        factory,
        clock,
        ids(),
        principal(),
        extractor=extractor,
        policy_version="formation@11",
        people_enabled=True,
    )
    result = await service.run(
        trigger="idle", scope="user", session_id=SESSION_ID, since_watermark=first
    )
    assert len(provider.requests) == 3
    if history_only or source_isolated:
        assert not result.beliefs
    else:
        assert [belief.statement for belief in result.beliefs] == [statement]
        assert result.beliefs[0].source_event_ids == [first, current]
        assert result.beliefs[0].valid_from == clock.now()


@pytest.mark.parametrize("text_size", [20, 3000, 9000])
async def test_context_is_complete_bounded_and_owner_only(text_size: int) -> None:
    from agent_core.domain.events import NewEvent
    from agent_core.domain.messages import TextPart, UserMessage

    clock, factory = await memory_uow_factory()
    for _ in range(12):
        await user_event(factory, "Earlier topic " + "x" * text_size)
    async with factory() as uow:
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="user.message.created",
                actor_type="principal",
                actor_id="someone-else",
                payload={"content": "Foreign context must not appear"},
            )
        )
        await uow.events.append(
            NewEvent(
                session_id=SESSION_ID,
                run_id=None,
                event_type="assistant.message.completed",
                actor_type="model",
                payload={"content": "Assistant context must not appear"},
            )
        )
    current = await user_event(factory, "It takes 18 seconds to start.")
    async with factory() as uow:
        events = await uow.events.list_after(SESSION_ID, current - 1, principal())
    provider = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text="{}") for _ in range(3)]), clock
    )
    extractor = PeopleAssistedCandidateExtractor(
        provider=provider,
        resolved_model=ResolvedModel(provider="fake", model="scripted", resolved_at=clock.now()),
        uow_factory=factory,
        clock=clock,
        ids=ids(),
    )
    await extractor.extract(events, principal=principal(), scope="user")
    payload = json.loads(
        next(
            part.text
            for message in provider.requests[2].conversation
            if isinstance(message, UserMessage)
            for part in message.content
            if isinstance(part, TextPart)
        )
    )
    context = payload["context_events"]
    assert len(context) == {20: 8, 3000: 2, 9000: 0}[text_size]
    assert sum(len(item["text"].encode()) for item in context) <= 8192
    assert all(item["text"] == "Earlier topic " + "x" * text_size for item in context)
    assert all(item["source_event_id"] < current for item in context)
    assert [item["source_event_id"] for item in payload["source_events"]] == [current]
