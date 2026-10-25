"""Review reasons and the two ranking controls (ADR-0249, plan ADR-B part 1).

A belief is flagged for one of four reasons, derived from what the store
already records: a conflict link, a correction, source learning, or an
automatically captured sensitive statement. The first two are doubt and take
`retrieval.flagged_penalty`; the last two only ask for attention and take
`retrieval.attention_penalty`, which is what lets a website-learned fact
surface as an attributed tentative line without ever outranking the owner's
own words (the ordering invariant below) or crowding them out (the one-third
cap).
"""

from __future__ import annotations

import math
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_core.adapters.determinism import RandomIdFactory
from agent_core.domain.memory import (
    MemoryAuthority,
    MemoryDerivation,
    MemoryLongevity,
    MemoryRecord,
    MemoryStatus,
    RecalledBelief,
    RecallQuery,
    Sensitivity,
    review_reason,
)
from agent_core.memory.profiles import RetrievalProfile
from agent_core.memory.retrieval import HybridMemoryRetriever, _score
from tests.contract.memory_fixtures import memory, recall_query
from tests.contract.support import PRINCIPAL_ID, SESSION_ID, TENANT, memory_uow_factory, principal


def _learned(belief_id: int = 601, **update: object) -> MemoryRecord:
    base: dict[str, object] = {
        "statement": "Source suggests: concise answers are preferred [Source: example.com]",
        "status": MemoryStatus.PROVISIONAL,
        "confidence": 0.35,
        "authority": MemoryAuthority.INFERRED,
        "derivation": MemoryDerivation.HYPOTHESIS,
        "longevity": MemoryLongevity.TENTATIVE,
        "sensitivity": Sensitivity.SENSITIVE,
        "flagged_for_review": True,
        "consolidation_policy_version": "owner-source-learning@1",
    }
    return memory(belief_id=belief_id).model_copy(update={**base, **update})


def test_review_reason_is_derived_from_what_the_record_records() -> None:
    assert review_reason(memory()) is None
    assert review_reason(memory().model_copy(update={"flagged_for_review": True})) == "correction"
    assert (
        review_reason(
            memory().model_copy(
                update={"flagged_for_review": True, "conflicts_with": [UUID(int=9)]}
            )
        )
        == "conflict"
    )
    assert review_reason(_learned()) == "source_learned"
    assert (
        review_reason(
            memory().model_copy(
                update={
                    "flagged_for_review": True,
                    "authority": MemoryAuthority.INFERRED,
                    "sensitivity": Sensitivity.RESTRICTED,
                }
            )
        )
        == "sensitive_automatic"
    )
    # An unflagged link is still a conflict to show; an unflagged sensitive
    # inference is nothing to review.
    assert (
        review_reason(memory().model_copy(update={"conflicts_with": [UUID(int=9)]})) == "conflict"
    )
    assert (
        review_reason(
            memory().model_copy(
                update={"authority": MemoryAuthority.INFERRED, "sensitivity": Sensitivity.SENSITIVE}
            )
        )
        is None
    )


def test_penalties_follow_the_reason_and_the_knobs() -> None:
    query = recall_query()
    clean = _score(memory(), query)
    corrected = _score(memory().model_copy(update={"flagged_for_review": True}), query)
    attention = _score(
        memory().model_copy(
            update={
                "flagged_for_review": True,
                "consolidation_policy_version": "owner-source-learning@1",
            }
        ),
        query,
    )
    assert clean is not None and corrected is not None and attention is not None
    assert corrected.score == pytest.approx(clean.score - 0.2)
    assert attention.score == pytest.approx(clean.score - 0.05)
    assert attention.review_reason == "source_learned" and corrected.review_reason == "correction"

    tuned = RetrievalProfile(flagged_penalty=0.3, attention_penalty=0.08)
    assert _score(
        memory().model_copy(update={"flagged_for_review": True}), query, profile=tuned
    ).score == pytest.approx(clean.score - 0.3)  # type: ignore[union-attr]
    assert _score(
        memory().model_copy(
            update={
                "flagged_for_review": True,
                "consolidation_policy_version": "owner-source-learning@1",
            }
        ),
        query,
        profile=tuned,
    ).score == pytest.approx(clean.score - 0.08)  # type: ignore[union-attr]


@settings(max_examples=300, deadline=None, derandomize=True)
@given(
    user_rank=st.integers(min_value=1, max_value=61),
    hypothesis_rank=st.integers(min_value=1, max_value=5000),
    hypothesis_confidence=st.floats(min_value=0.2, max_value=0.55),
)
def test_ordering_invariant_under_the_shipped_profile(
    user_rank: int, hypothesis_rank: int, hypothesis_confidence: float
) -> None:
    """(i) at equal match an attention-flagged hypothesis trails an active user
    belief by at least 0.10; (ii) a user belief with match >= 0.5 outranks any
    attention-flagged hypothesis whatever its match. Match is driven through the
    semantic rank, (k+1)/(k+rank) with k = 60, so rank 61 is exactly 0.5."""

    profile = RetrievalProfile()
    query = recall_query(text=None)
    stated = memory(belief_id=701)
    learned = _learned(702, confidence=hypothesis_confidence)
    user = _score(stated, query, profile=profile, semantic_rank=user_rank)
    hypothesis = _score(learned, query, profile=profile, semantic_rank=hypothesis_rank)
    assert user is not None and hypothesis is not None
    assert user.score > hypothesis.score
    same_match = _score(learned, query, profile=profile, semantic_rank=user_rank)
    assert same_match is not None
    assert user.score - same_match.score >= 0.10 - 1e-9


async def test_attention_flagged_hypotheses_fill_at_most_a_third_of_the_slots() -> None:
    clock, factory = await memory_uow_factory()
    async with factory() as uow:
        stated = memory(belief_id=801, statement="User prefers concise answers in reviews.")
        await uow.memories.upsert_belief(
            stated.model_copy(update={"store_position": await uow.memories.next_position()})
        )
        for index in range(8):
            learned = _learned(
                810 + index,
                subject=f"source-{index}",
                statement=f"Source suggests: concise answers matter {index} [Source: a.com]",
            )
            await uow.memories.upsert_belief(
                learned.model_copy(update={"store_position": await uow.memories.next_position()})
            )
    retriever = HybridMemoryRetriever(factory, clock, RandomIdFactory(), principal())
    result = await retriever.recall(
        RecallQuery(
            tenant_id=TENANT,
            principal_id=PRINCIPAL_ID,
            current_scope="project-a",
            text="concise answers",
            budget_tokens=4000,
            max_items=6,
            min_score=0.0,
        ),
        session_id=SESSION_ID,
        turn_id=UUID(int=81),
    )
    items = [item for item in result.items if isinstance(item, RecalledBelief)]
    assert any(item.belief_id == stated.id for item in items)
    attention = [item for item in items if item.review_reason == "source_learned"]
    assert 1 <= len(attention) <= math.ceil(6 / 3)
