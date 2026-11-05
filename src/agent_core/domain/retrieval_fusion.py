"""Reciprocal rank fusion shared by belief recall and knowledge passage search.

memory-retrieval-and-ranking.md ("4. Fuse") fixes the rule: arms are ranked
independently and combined as the sum over arms of 1 / (k + rank), k = 60, so
no score has to be calibrated against another. The sum is normalised by the
best value any candidate could reach, first in every arm, so a fused match
reads on the same 0..1 scale as the arm scores it is blended with.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence

RECIPROCAL_RANK_FUSION_K = 60


def reciprocal_rank_fusion[Key: Hashable](
    rankings: Mapping[str, Sequence[Key]], *, k: int = RECIPROCAL_RANK_FUSION_K
) -> dict[Key, float]:
    """Fuse each arm's ordered candidates into one normalised score per candidate.

    ``rankings`` maps an arm name to its candidates, best first. A candidate an
    arm did not return contributes nothing for that arm. Only arms that
    returned something count towards the ceiling, as belief recall counts
    them, so a quiet arm never halves every score.
    """

    if k < 1:
        raise ValueError("reciprocal rank fusion needs k >= 1")
    present = {arm: members for arm, members in rankings.items() if members}
    totals: dict[Key, float] = {}
    for members in present.values():
        for rank, key in enumerate(members, 1):
            totals[key] = totals.get(key, 0.0) + 1 / (k + rank)
    ceiling = max(1, len(present)) / (k + 1)
    return {key: total / ceiling for key, total in totals.items()}


def blend_with_arm_score(score: float, fused: float, *, weight: float) -> float:
    """Weigh a candidate's own arm score against its fused rank, capped at one."""

    return min(1.0, (1 - weight) * score + weight * fused)
