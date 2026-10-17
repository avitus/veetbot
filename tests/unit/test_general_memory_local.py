from typing import Any

import pytest

from agent_core.evals.general_memory_local import summarize


def observation(**changes: Any) -> dict[str, Any]:
    return {
        "scenario_id": "synthetic",
        "family": "preferences",
        "candidate_rank": 1,
        "dense_rank": 1,
        "ranked_rank": 1,
        "rendered_rank": 1,
        "tokens": 100,
        "elapsed_ms": 5.0,
        **changes,
    }


def test_summary_attributes_actual_candidate_rank_and_budget_failures() -> None:
    rows = [
        observation(),
        observation(
            scenario_id="candidate", candidate_rank=None, ranked_rank=None, rendered_rank=None
        ),
        observation(scenario_id="ranking", ranked_rank=21, rendered_rank=None),
        observation(scenario_id="budget", ranked_rank=2, rendered_rank=None),
    ]
    report = summarize(rows)
    assert report["overall"]["rendered_recall_at_20"] == 0.25
    assert report["overall"]["ranked_recall_at_20"] == 0.5
    assert report["overall"]["failures"] == {"candidate": 1, "ranking": 1, "context_budget": 1}
    assert report["families"]["preferences"] == report["overall"]


@pytest.mark.parametrize("rows", [[], [observation(), observation()]])
def test_empty_or_duplicate_scenarios_cannot_supply_a_diagnostic(
    rows: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError, match="scenario"):
        summarize(rows)
