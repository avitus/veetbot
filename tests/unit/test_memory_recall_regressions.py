"""Production-derived recall failures, using synthetic personal information."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.context import WorkingState
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryStatus,
    Portability,
    RecallProfile,
    RecallQuery,
    Sensitivity,
    recall_query_terms,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn, TextPart, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.memory.retrieval import DeterministicQueryFormer, _score
from agent_core.tools.memory_search import MemorySearchTool
from tests.contract.memory_fixtures import formation_stack, memory, recall_query
from tests.contract.support import NOW, SESSION_ID, principal, run, tool_context
from tests.integration.m2_support import memory_settings

HYDRANGEA_QUESTION = "Is it worth adding iron fertilizer to my hydrangeas this late in the season?"


@pytest.mark.parametrize(
    ("question", "statement", "belief_type"),
    [
        (
            "Which vehicle do I drive?",
            "User drives an electric cargo bike.",
            BeliefType.USER_MODEL_ATTR,
        ),
        ("Who is my partner?", "User's wife is Casey.", BeliefType.RELATIONSHIP),
        (
            "Which unit system do I prefer?",
            "User prefers metric measurements.",
            BeliefType.PREFERENCE,
        ),
        ("What did I buy last month?", "User owns a canoe.", BeliefType.USER_MODEL_ATTR),
    ],
)
async def test_explicit_personal_recall_uses_structured_profile_as_well_as_words(
    question: str, statement: str, belief_type: BeliefType
) -> None:
    _clock, factory, _service, retriever = await formation_stack()
    record = memory(statement=statement).model_copy(
        update={
            "subject": "personal detail",
            "belief_type": belief_type,
            "status": MemoryStatus.PROVISIONAL,
        }
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    queries = DeterministicQueryFormer(principal()).form(run(), WorkingState(), question)
    result = await retriever.recall(queries[0], session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [record.id]
    assert "structured" in result.items[0].arms


@pytest.mark.parametrize(
    "question",
    [
        HYDRANGEA_QUESTION,
        "What should I plant?",
        "Which car should I buy?",
        "Who is their manager?",
    ],
)
async def test_advice_and_third_party_questions_do_not_browse_personal_profile(
    question: str,
) -> None:
    _clock, factory, _service, retriever = await formation_stack()
    record = memory(statement="The user wears a green scarf.")
    async with factory() as uow:
        await uow.memories.upsert_belief(record)
    query = DeterministicQueryFormer(principal()).form(run(), WorkingState(), question)[0]
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert result.items == []


@pytest.mark.parametrize("question", [HYDRANGEA_QUESTION, "the this", "THE, this!"])
async def test_recall_does_not_return_memories_for_common_words(question: str) -> None:
    _clock, factory, _service, retriever = await formation_stack()
    noise = memory(statement="The user enabled this repository's notification emails.")
    async with factory() as uow:
        await uow.memories.upsert_belief(noise)
        candidates = await uow.memories.query(recall_query(text=question))
    assert candidates == []
    assert _score(noise, recall_query(text=question), now=NOW) is None
    result = await retriever.recall(recall_query(text=question), session_id=SESSION_ID)
    assert result.items == []


async def test_content_and_explicit_subject_matches_survive_common_word_filter() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    plant = memory(statement="The user grows hydrangeas.")
    named = memory(belief_id=503, statement="An explicitly named subject.").model_copy(
        update={"subject": "The", "store_position": 2}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(plant)
        await uow.memories.upsert_belief(named)
    result = await retriever.recall(recall_query(text=HYDRANGEA_QUESTION), session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [plant.id]
    result = await retriever.recall(
        recall_query(text="the this", subjects=["The"]), session_id=SESSION_ID
    )
    assert [item.belief_id for item in result.items] == [named.id]


async def test_snapshot_excludes_provisional_but_deliberate_and_delta_recall_keep_it() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    provisional = memory(statement="User lives in Exampleville.").model_copy(
        update={"status": MemoryStatus.PROVISIONAL, "subject": "home location"}
    )
    async with factory() as uow:
        await uow.memories.upsert_belief(provisional)
    snapshot = await retriever.snapshot(session_id=SESSION_ID, current_scope="project-a")
    assert snapshot.items == []
    delta = await retriever.recall(
        recall_query(text=None, profile=RecallProfile.CORE),
        session_id=SESSION_ID,
        moment="in_turn",
    )
    assert [item.belief_id for item in delta.items] == [provisional.id]
    result = await MemorySearchTool(retriever).execute(
        {"text": "home location city residence", "scope": "project-a"}, tool_context()
    )
    assert result.structured is not None
    assert result.structured["belief_ids"] == [str(provisional.id)]
    assert result.output_trust is TrustLevel.MEMORY


async def test_hydrangea_model_request_has_address_despite_provisional_candidate_flood(
    tmp_path: Path,
) -> None:
    """Exercise the real planner, query former, stores, and request builder.

    The older confirmed project fact must survive more provisional profile
    beliefs than the store's 320-candidate snapshot cap. No model is scripted
    to request the address: it must already be in its first request.
    """
    async with build(
        settings=replace(memory_settings(), artifact_root=tmp_path / "artifacts"),
        storage="memory",
        script=FakeModelScript(turns=[ScriptedTurn(text="ack")], on_exhausted="repeat_last"),
    ) as app:
        source_session = await app.sessions.create()
        address = memory(
            belief_id=9000, statement="The home address is 123 Example Lane, Exampleville."
        ).model_copy(
            update={
                "tenant_id": app.principal.tenant_id,
                "principal_id": app.principal.principal_id,
                "source_session_id": source_session,
                "subject": "Example Owner",
                "scope": "veetbot",
                "origin_scopes": ["veetbot"],
                "belief_type": BeliefType.FACT,
                "portability": Portability.CONTEXTUAL,
                "authority": MemoryAuthority.AFFIRMED,
                "sensitivity": Sensitivity.RESTRICTED,
            }
        )
        noise = [
            address.model_copy(
                update={
                    "id": UUID(int=9100 + index),
                    "subject": f"notification preference {index}",
                    "statement": f"The user enabled repository {index} notification emails.",
                    "scope": "general",
                    "origin_scopes": ["general"],
                    "belief_type": BeliefType.USER_MODEL_ATTR,
                    "portability": Portability.PORTABLE,
                    "authority": MemoryAuthority.INFERRED,
                    "status": MemoryStatus.PROVISIONAL,
                    "confidence": 0.65,
                    "store_position": index + 2,
                }
            )
            for index in range(321)
        ]
        async with app.uow_factory() as uow:
            for record in [address, *noise]:
                await uow.memories.upsert_belief(record)
        session_id = await app.sessions.create()
        await app.runs.wait_terminal(await app.runs.submit(HYDRANGEA_QUESTION, session_id))
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        request = provider.requests[0]
        content = "\n".join(
            part.text
            for item in request.conversation
            if isinstance(item, UserMessage)
            for part in item.content
            if isinstance(part, TextPart)
        )
        assert address.statement in content
        assert not any(record.statement in content for record in noise)
        plan = await app.executor._context_planner.current(session_id)
        assert plan is not None and plan.snapshot_id is not None
        async with app.uow_factory() as uow:
            snapshot = await uow.traces.get(plan.snapshot_id, app.principal)
        assert snapshot.returned == [address.id]
        assert snapshot.candidates == 1
        assert snapshot.rendered == plan.memory_snapshot
        await app.runs.wait_terminal(await app.runs.submit("And next season?", session_id))
        assert provider.requests[-1].metadata["prefix_sha256"] == request.metadata["prefix_sha256"]


def test_memory_search_advertises_lookup_of_missing_personal_context() -> None:
    description = MemorySearchTool.spec.description
    assert "before asking" in description
    assert "home location" in description
    assert "snapshot" in description


def test_recall_filter_preserves_negation_names_and_non_english_terms() -> None:
    assert recall_query_terms("The user does not use Neovim or PostgreSQL") == [
        "neovim",
        "not",
        "postgresql",
        "use",
        "user",
    ]
    assert recall_query_terms("住所 東京") == ["住所 東京"]
    assert recall_query_terms("AI") == ["ai"]
    assert recall_query_terms("THE") == []


def test_historical_trace_queries_load_without_new_fields() -> None:
    payload = recall_query().model_dump(exclude={"include_provisional", "structured_belief_types"})
    loaded = RecallQuery.model_validate(payload)
    assert loaded.include_provisional is True
    assert loaded.structured_belief_types == []


async def test_structured_profile_preserves_scope_ceiling_and_ranking() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    matched = memory(statement="User prefers concise reports.")
    background = memory(belief_id=510, statement="User prefers morning meetings.")
    hidden = memory(belief_id=511, statement="User prefers concise reports privately.").model_copy(
        update={"sensitivity": Sensitivity.RESTRICTED}
    )
    foreign = memory(belief_id=512).model_copy(update={"principal_id": "someone-else"})
    async with factory() as uow:
        for record in [matched, background, hidden, foreign]:
            await uow.memories.upsert_belief(record)
    query = recall_query(
        text="concise reports", sensitivity_ceiling=Sensitivity.INTERNAL
    ).model_copy(update={"structured_belief_types": [BeliefType.PREFERENCE]})
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert [item.belief_id for item in result.items] == [matched.id, background.id]
    assert result.items[0].score > result.items[1].score
