"""Memory retrieval contract: recorded traces and fail-closed isolation."""

import hashlib
from uuid import UUID

from agent_core.domain.memory import MemoryStatus, RecallMoment
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION
from tests.contract.memory_fixtures import formation_stack, memory, recall_query
from tests.contract.support import PRINCIPAL_ID, SESSION_ID, TENANT, principal


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
    assert trace.retrieval_policy_version == RETRIEVAL_POLICY_VERSION


async def test_recall_for_another_identity_fails_closed_and_traces_the_caller() -> None:
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
        assert trace.query.principal_id == PRINCIPAL_ID


async def test_corrections_cover_closed_snapshot_members_and_nothing_else() -> None:
    """Corrections are principal-scoped, snapshot-scoped, and watermark-bounded."""

    clock, factory, _service, retriever = await formation_stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(memory())
    snapshot = await retriever.recall(
        recall_query(), session_id=SESSION_ID, moment=RecallMoment.SNAPSHOT.value
    )
    assert [item.belief_id for item in snapshot.items] == [memory().id]

    retired = memory().model_copy(
        update={
            "status": MemoryStatus.RETIRED,
            "valid_to": clock.now(),
            "store_position": snapshot.watermark + 1,
            "updated_at": clock.now(),
        }
    )
    async with factory() as uow:
        await uow.memories.reinforce(retired)

    corrections = await retriever.corrections(
        snapshot_id=snapshot.trace_id, watermark=snapshot.watermark
    )

    assert [item.belief_id for item in corrections] == [memory().id]
    assert corrections[0].replacement_id is None
    assert corrections[0].ended_at == clock.now()
    # A trace belonging to nobody this retriever can read is not a correction
    # source, and neither is a snapshot the closure predates.
    assert await retriever.corrections(snapshot_id=UUID(int=7_777), watermark=0) == []
    assert (
        await retriever.corrections(snapshot_id=snapshot.trace_id, watermark=retired.store_position)
        == []
    )


async def test_recall_excludes_persona_promoted_beliefs() -> None:
    _clock, factory, _service, retriever = await formation_stack()
    kept = memory()
    promoted = memory(belief_id=502, statement="User prefers direct feedback")
    async with factory() as uow:
        await uow.memories.upsert_belief(kept)
        await uow.memories.upsert_belief(
            promoted.model_copy(update={"subject": "feedback style", "store_position": 2})
        )

    plain = await retriever.recall(
        recall_query(text="prefers"), session_id=SESSION_ID, moment="in_turn"
    )
    assert {belief.belief_id for belief in plain.items} == {kept.id, promoted.id}

    excluded = await retriever.recall(
        recall_query(text="prefers", exclude_ids=(promoted.id,)),
        session_id=SESSION_ID,
        moment="in_turn",
    )
    excluded_ids = {belief.belief_id for belief in excluded.items}
    assert kept.id in excluded_ids
    assert promoted.id not in excluded_ids
    assert "direct feedback" not in excluded.rendered
