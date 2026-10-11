"""Local query vectors cannot widen retrieval admission or cross query identity."""

from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.domain.memory import RecallQuery
from agent_core.domain.semantic_memory import SemanticQueryVector
from agent_core.memory.profiles import DEFAULT_RETRIEVAL_PROFILE
from agent_core.memory.retrieval import HybridMemoryRetriever
from tests.contract.memory_fixtures import formation_stack, memory
from tests.contract.semantic_memory_cases import SPACE, VECTOR
from tests.contract.support import SESSION_ID, principal


@pytest.mark.parametrize(
    "mode", ["disabled", "wrong_query", "history", "core", "foreign", "people", "legacy"]
)
async def test_semantic_lookup_cannot_widen_admission(mode: str) -> None:
    from agent_core.domain.memory import RecallProfile
    from tests.contract.memory_fixtures import recall_query

    clock, factory, _, _ = await formation_stack()
    calls = []

    class Lookup:
        def lookup(self, query: RecallQuery) -> SemanticQueryVector:
            calls.append(query)
            return SemanticQueryVector(query_sha256="0" * 64, space=SPACE, vector=VECTOR)

    async with factory() as uow:
        original = await uow.memories.upsert_belief(
            memory(statement="Dairy causes digestive discomfort")
        )
        source = await uow.semantic_memory.capture(principal(), original.id, SPACE)
        assert source is not None and await uow.semantic_memory.put(principal(), source, VECTOR)
    query = recall_query(text="Breakfast ideas").model_copy(
        update={"candidate_selection": "relevance"}
    )
    updates: dict[str, dict[str, object]] = {
        "history": {"known_at": clock.now()},
        "core": {"profile": RecallProfile.CORE},
        "foreign": {"principal_id": "other"},
        "people": {"people_scope": (UUID(int=912),)},
        "legacy": {"candidate_selection": "recent"},
    }
    query = query.model_copy(update=updates.get(mode, {}))
    retriever = HybridMemoryRetriever(
        factory,
        clock,
        SequenceIdFactory(UUID(int=i) for i in range(8000, 8010)),
        principal(),
        profile=DEFAULT_RETRIEVAL_PROFILE.model_copy(
            update={"semantic_enabled": mode != "disabled"}
        ),
        semantic_queries=Lookup(),
    )
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert result.items == []
    assert len(calls) == (1 if mode == "wrong_query" else 0)
