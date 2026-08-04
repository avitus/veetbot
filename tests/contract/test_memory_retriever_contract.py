"""Memory retrieval interface contract."""

import inspect

from agent_core.memory.retrieval import HybridMemoryRetriever


def test_hybrid_retriever_exposes_async_recall_and_snapshot() -> None:
    assert inspect.iscoroutinefunction(HybridMemoryRetriever.recall)
    assert inspect.iscoroutinefunction(HybridMemoryRetriever.snapshot)
