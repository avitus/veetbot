"""A current status answer should identify the subject the question names."""

from datetime import timedelta

import pytest

from agent_core.domain.memory import MemoryAuthority, MemoryClaimKind, MemoryStatus, RecallProfile
from tests.contract.memory_fixtures import formation_stack, memory, recall_query
from tests.contract.support import NOW, SESSION_ID


@pytest.mark.parametrize("profile", [RecallProfile.TASK, RecallProfile.DEEP])
@pytest.mark.parametrize("negative", [False, True])
async def test_named_current_state_excludes_an_older_opposing_inference(
    profile: RecallProfile, negative: bool
) -> None:
    _, factory, _, retriever = await formation_stack()
    other = memory(statement=f"The west vault is {'not ' if negative else ''}sealed.").model_copy(
        update={
            "subject": "west vault",
            "authority": MemoryAuthority.INFERRED,
            "status": MemoryStatus.PROVISIONAL,
            "claim_kind": MemoryClaimKind.RECURRING_STATE,
            "valid_from": NOW - timedelta(days=2),
        }
    )
    confirmed = memory(
        belief_id=502,
        statement=f"The east vault is {'available and is not ' if not negative else ''}sealed.",
    ).model_copy(update={"subject": "east vault inspection", "authority": MemoryAuthority.AFFIRMED})
    async with factory() as uow:
        for row in (other, confirmed):
            await uow.memories.upsert_belief(row)
    query = recall_query(text="Is the east vault sealed?", profile=profile, min_score=0).model_copy(
        update={"candidate_selection": "relevance"}
    )
    result = await retriever.recall(query, session_id=SESSION_ID)
    assert confirmed.id in {item.belief_id for item in result.items}
    assert other.id not in {item.belief_id for item in result.items}

    # After a deliberate search, automatic recall excludes the fact already
    # shown. A partial subject match must not reintroduce the other status.
    remainder = await retriever.recall(
        query.model_copy(update={"exclude_ids": (confirmed.id,)}), session_id=SESSION_ID
    )
    assert other.id not in {item.belief_id for item in remainder.items}


@pytest.mark.parametrize(
    ("question", "statement", "subject", "excluded"),
    [
        ("Are the bronze kilns hot?", "The silver kiln is currently hot.", "silver kiln", True),
        ("Is my silver kiln hot?", "The silver kiln is currently hot.", "silver kiln", False),
        ("Are any kilns hot?", "The silver kiln is hot.", "silver kiln", False),
        ("Are all kilns hot?", "The silver kiln is hot.", "silver kiln", False),
        ("Is the kiln hot?", "The silver kiln is currently hot.", "silver kiln", False),
        ("Is the bronze kiln hot?", "The kiln is hot.", "kiln", False),
        ("Is the bronze kiln hot?", "The pump is hot.", "pump", False),
        ("Are the bronze and silver kilns hot?", "The blue kiln is hot.", "blue kiln", False),
        ("Was my bronze kiln hot?", "The silver kiln is currently hot.", "silver kiln", False),
        ("Is the bronze kiln hot?", "The silver kiln is idle.", "silver kiln", False),
        ("Is the bronze kiln hot?", "If the silver kiln is hot.", "silver kiln", False),
        ("Is the bronze kiln hot?", "The bronze kiln is hot.", "silver kiln", False),
        ("Is the bronze kiln hot?", "The silver kiln is hot only at night.", "silver kiln", False),
    ],
)
def test_scope_of_atomic_state_questions(
    question: str, statement: str, subject: str, excluded: bool
) -> None:
    from uuid import UUID

    from agent_core.memory.state_questions import grounded_state_beliefs
    from tests.unit.test_context_recall_delta import _belief

    query = recall_query(text=question).model_copy(update={"candidate_selection": "relevance"})
    item = _belief(UUID(int=75), statement).model_copy(
        update={
            "subject": subject,
            "status": MemoryStatus.PROVISIONAL,
            "authority": MemoryAuthority.INFERRED,
            "claim_kind": MemoryClaimKind.RECURRING_STATE,
        }
    )
    assert grounded_state_beliefs(query, [item]) == ([] if excluded else [item])
    if excluded:
        for guarded in (
            query.model_copy(update={"as_of": NOW}),
            query.model_copy(update={"known_at": NOW}),
            query.model_copy(update={"candidate_selection": "recent"}),
            query.model_copy(update={"include_ids": (item.belief_id,)}),
            query.model_copy(update={"profile": RecallProfile.CORE}),
        ):
            assert grounded_state_beliefs(guarded, [item]) == [item]
        for kept in (
            item.model_copy(update={"authority": MemoryAuthority.USER}),
            item.model_copy(update={"authority": MemoryAuthority.AFFIRMED}),
            item.model_copy(update={"status": MemoryStatus.ACTIVE}),
            item.model_copy(update={"claim_kind": MemoryClaimKind.HABIT}),
            item.model_copy(update={"conflict_with": [UUID(int=76)]}),
        ):
            assert grounded_state_beliefs(query, [kept]) == [kept]
        partner = _belief(UUID(int=76), "A linked observation.").model_copy(
            update={"conflict_with": [item.belief_id]}
        )
        assert grounded_state_beliefs(query, [item, partner]) == [item, partner]
