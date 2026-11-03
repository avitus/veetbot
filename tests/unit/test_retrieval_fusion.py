"""Reciprocal rank fusion is one rule, shared by belief recall and knowledge search."""

from __future__ import annotations

import pytest

from agent_core.domain.retrieval_fusion import (
    RECIPROCAL_RANK_FUSION_K,
    blend_with_arm_score,
    reciprocal_rank_fusion,
)


def test_fusion_matches_the_spec_formula_and_normalises_to_the_two_arm_ceiling() -> None:
    k = RECIPROCAL_RANK_FUSION_K
    fused = reciprocal_rank_fusion({"lexical": ["a", "b"], "semantic": ["b", "c"]})
    ceiling = 2 / (k + 1)
    assert fused["b"] == pytest.approx((1 / (k + 2) + 1 / (k + 1)) / ceiling)
    assert fused["a"] == pytest.approx((1 / (k + 1)) / ceiling)
    assert fused["c"] == pytest.approx((1 / (k + 2)) / ceiling)
    assert fused["b"] > fused["a"] > fused["c"]


def test_an_arm_that_returned_nothing_does_not_halve_every_score() -> None:
    fused = reciprocal_rank_fusion({"lexical": ["a"], "semantic": []})
    assert fused["a"] == pytest.approx(1.0)


def test_consensus_outranks_a_single_first_place() -> None:
    # The property ADR-0289 measured: a candidate both arms return beats one
    # only the semantic arm ranks first, whatever its lexical rank.
    fused = reciprocal_rank_fusion({"lexical": ["junk", "x", "y"], "semantic": ["answer", "junk"]})
    assert fused["junk"] > fused["answer"]


def test_blend_and_bounds() -> None:
    assert blend_with_arm_score(0.5, 1.0, weight=0.2) == pytest.approx(0.6)
    assert blend_with_arm_score(1.0, 1.0, weight=0.2) == 1.0
    with pytest.raises(ValueError, match="k >= 1"):
        reciprocal_rank_fusion({"lexical": ["a"]}, k=0)
