"""Memory retrieval contract: recorded traces and fail-closed isolation."""

import hashlib
import inspect
from uuid import UUID

from agent_core.memory.retrieval import HybridMemoryRetriever
from tests.contract.memory_fixtures import formation_stack, memory, recall_query
from tests.contract.support import SESSION_ID, TENANT, principal


def test_hybrid_retriever_exposes_async_recall_and_snapshot() -> None:
    assert inspect.iscoroutinefunction(HybridMemoryRetriever.recall)
    assert inspect.iscoroutinefunction(HybridMemoryRetriever.snapshot)


async def test_recall_records_a_trace_bound_to_the_rendered_bytes() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    turn_id = UUID(int=91)

    result = await retriever.recall(
        recall_query(),
        session_id=SESSION_ID,
        turn_id=turn_id,
        moment="in_turn",
    )

    async with factory() as uow:
        traces = await uow.traces.for_turn(turn_id)
    assert len(traces) == 1
    trace = traces[0]
    assert trace.id == result.trace_id
    assert trace.rendered == result.rendered
    assert trace.rendered_sha256 == hashlib.sha256(result.rendered.encode()).hexdigest()
    assert trace.returned == [item.belief_id for item in result.items]
    assert trace.retrieval_policy_version == "retrieval@1"


async def test_recall_for_another_identity_fails_closed_without_reaching_the_store() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())

    for query in (
        recall_query(tenant_id="tenant-b"),
        recall_query(principal_id="principal-b"),
    ):
        result = await retriever.recall(query, session_id=SESSION_ID)
        assert result.items == []
        async with factory() as uow:
            trace = await uow.traces.get(result.trace_id, principal())
        assert trace.candidates == 0
        assert trace.query.tenant_id == TENANT
