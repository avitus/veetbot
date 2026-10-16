"""The evaluator receives stage IDs without exposing them as memory evidence."""

from agent_core.domain.memory import Sensitivity
from tests.contract.memory_fixtures import formation_stack, memory, recall_query
from tests.contract.support import SESSION_ID


async def test_candidate_rank_and_budget_stages_are_distinct_and_not_serialized() -> None:
    _, factory, _, retriever = await formation_stack()
    originals = [
        memory(belief_id=501 + i, statement=f"User prefers early breakfast option {i}").model_copy(
            update={"store_position": i + 1}
        )
        for i in range(3)
    ]
    async with factory() as uow:
        for original in originals:
            await uow.memories.upsert_belief(original)
    query = recall_query(text="breakfast").model_copy(update={"max_items": 1})
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert result.stages is not None, "Evaluation must observe the real retrieval stages"
    assert set(result.stages.candidate_ids) == {item.id for item in originals}
    assert set(result.stages.ranked_ids) == set(result.stages.candidate_ids)
    assert len(result.items) == 1 and len(result.stages.ranked_ids) == 3
    assert result.items[0].belief_id == result.stages.ranked_ids[0]
    assert "stages" not in result.model_dump() and "candidate_ids" not in result.model_dump_json()
    assert "candidate_ids" not in repr(result)


async def test_observation_never_includes_invisible_candidates() -> None:
    _, factory, _, retriever = await formation_stack()
    async with factory() as uow:
        await uow.memories.upsert_belief(
            memory().model_copy(update={"sensitivity": Sensitivity.RESTRICTED})
        )
    for query in [
        recall_query(tenant_id="foreign"),
        recall_query().model_copy(update={"sensitivity_ceiling": Sensitivity.PUBLIC}),
    ]:
        result = await retriever.recall(query, session_id=SESSION_ID)
        assert result.stages is not None
        assert (
            result.stages.candidate_ids == result.stages.ranked_ids == result.stages.dense_ids == ()
        )
