"""Versioned visible knowledge-store contract."""

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
from agent_core.domain.knowledge import KnowledgeQuery
from tests.contract.memory_fixtures import prepared_knowledge
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT


async def test_knowledge_store_ingests_and_hard_filters_visibility() -> None:
    store = InMemoryKnowledgeStore(FixedClock(NOW))
    prepared = prepared_knowledge()
    await store.ingest(prepared)
    query = KnowledgeQuery(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        current_scope=None,
        text="restart service",
        budget_tokens=500,
        max_passages=5,
        min_score=0.1,
    )
    assert await store.search(query)
    assert await store.search(query.model_copy(update={"principal_id": "other"})) == []
