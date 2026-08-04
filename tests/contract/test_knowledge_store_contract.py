"""Versioned visible knowledge-store contract."""

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.memory.in_memory import InMemoryKnowledgeStore
from agent_core.domain.knowledge import KnowledgeDocument, KnowledgeQuery
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
    assert await store.search(query.model_copy(update={"tenant_id": "other"})) == []
    assert len(await store.search(query.model_copy(update={"max_passages": 1}))) == 1
    assert await store.search(query.model_copy(update={"min_score": 1.0})) == []
    assert await store.search(query.model_copy(update={"text": "restart,service!"}))


def test_knowledge_document_rejects_an_inverted_validity_window() -> None:
    document = prepared_knowledge().document
    payload = document.model_dump(mode="python")
    payload["valid_to"] = document.valid_from.replace(year=document.valid_from.year - 1)
    with pytest.raises(ValueError, match="precedes"):
        KnowledgeDocument.model_validate(payload)
