"""Deterministic recall reranker contract."""

from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryStatus,
    Portability,
    RecalledBelief,
    Sensitivity,
)
from agent_core.memory.retrieval import HandWeightedRanker
from tests.contract.memory_fixtures import memory, recall_query


def test_ranker_is_score_then_identity_deterministic() -> None:
    base = memory()
    items = [
        RecalledBelief(
            belief_id=base.id,
            subject=base.subject,
            statement=base.statement,
            belief_type=BeliefType.PREFERENCE,
            status=MemoryStatus.ACTIVE,
            confidence_band="high",
            authority=MemoryAuthority.USER,
            origin_scope="project-a",
            portability=Portability.PORTABLE,
            sensitivity=Sensitivity.INTERNAL,
            valid_from=base.valid_from,
            score=score,
            arms=["lexical"],
        )
        for score in (0.2, 0.8)
    ]
    assert HandWeightedRanker().rank(items, recall_query())[0].score == 0.8
