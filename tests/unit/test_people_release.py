"""Activation publication preserves failed repeats and separate owner acceptance."""

from copy import deepcopy
from typing import Any

import pytest

from tests.contract.support import NOW


def passing_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    metadata = {
        "schema_version": 1,
        "state": "completed",
        "build_ref": "a" * 40,
        "corpus_sha256": {"development": "1" * 64, "holdout": "2" * 64},
        "ordinary_corpus_sha256": "3" * 64,
        "ordinary_holdout_sha256": "4" * 64,
        "schema_sha256": "5" * 64,
        "implementation_sha256": "6" * 64,
        "provider": "fixture",
        "model": "fixture",
        "model_policy": "fixture",
        "policy_profile": "default",
        "policy_version": "policy@1",
        "reasoning_configuration": "provider-default",
        "repeats": 3,
        "review_status": ["reviewed", "reviewed"],
        "reserved_usd": "0",
    }
    summaries = []
    ordinary = []
    for split, cases in (("development", 120), ("holdout", 60)):
        for repeat in range(3):
            summaries.append(
                {
                    "split": split,
                    "pipeline": "full-people",
                    "repeat": repeat,
                    "cases": cases,
                    "collision_cases": cases,
                    "identity_precision": 1,
                    "resolvable_identity_recall": 1,
                    "collision_false_merges": 0,
                    "direct_fact_recall": 1,
                    "direct_fact_precision": 1,
                    "direction_accuracy": 1,
                    "attribution_accuracy": 1,
                    "temporal_accuracy": 1,
                    "task_success": 1,
                    "retrieval_evidence_recall": 1,
                    "abstentions": 0,
                    "unambiguous_decisions": cases * 10,
                    "cost_usd": "1",
                    "formation_calls": cases * 3,
                    "formation_segments": cases,
                    "formation_failures": 0,
                    "formation_call_mismatches": 0,
                }
            )
            ordinary.append(
                {
                    "split": split,
                    "repeat": repeat,
                    "ordinary_memory_regressions": 0,
                    "boundary_failures": 0,
                    "formation_call_mismatches": 0,
                    "candidate": {
                        "direct_must_form_recall": 1,
                        "hypothesis_must_form_recall": 1,
                        "benign_precision": 1,
                        "evidence_disposition_precision": 1,
                    },
                }
            )
    scores = {
        "summaries": summaries,
        "repeats": 3,
        "paired_improvement_ci95": [0.12, 0.19],
        "scorer_version": "people-scorer@1",
    }
    acceptance = {
        "schema_version": 1,
        "build_ref": "a" * 40,
        "run_sha256": "7" * 64,
        "boundary_failures": 0,
        "owner_people_count": 25,
        "owner_task_count": 50,
        "owner_useful_correct": 0.96,
        "owner_harmful_mixups": 0,
        "evaluated_at": NOW.isoformat(),
    }
    return metadata, scores, {"reports": ordinary}, acceptance


@pytest.mark.parametrize(
    "failure", [None, "repeat", "ordinary", "unreviewed", "charge", "owner", "build"]
)
def test_publication_requires_every_repeat_and_owner_acceptance(failure: str | None) -> None:
    from agent_core.evals.people_release import assemble_evidence

    metadata, scores, ordinary, acceptance = deepcopy(passing_inputs())
    if failure == "repeat":
        scores["summaries"][-1]["task_success"] = 0.89
    elif failure == "ordinary":
        ordinary["reports"][0]["ordinary_memory_regressions"] = 1
    elif failure == "unreviewed":
        metadata["review_status"][-1] = "unreviewed"
    elif failure == "charge":
        metadata["reserved_usd"] = "0.01"
    elif failure == "owner":
        acceptance["owner_harmful_mixups"] = 1
    elif failure == "build":
        acceptance["build_ref"] = "b" * 40
    if failure:
        with pytest.raises(ValueError):
            assemble_evidence(metadata, scores, ordinary, acceptance, labeled_mentions=1800)
    else:
        result = assemble_evidence(metadata, scores, ordinary, acceptance, labeled_mentions=1800)
        assert len(result.run_metrics) == len(result.holdout_metrics) == 3
        assert result.run_metrics[0].provider_calls == 360
        assert result.owner_useful_correct == 0.96
        assert result.ordinary_holdout_sha256 == "4" * 64


@pytest.mark.parametrize(
    "failure", [None, "uncertain", "cost", "model", "duplicate", "observed_calls", "observed_cost"]
)
def test_publication_verifies_provider_journal(tmp_path: Any, failure: str | None) -> None:
    import json

    from agent_core.evals.people_release import verify_cost_journal

    metadata = {
        "provider": "fixture",
        "model": "fixture",
        "provider_calls": 1,
        "spent_usd": "0.04",
        "maximum_cost_usd": "1",
        "reserved_usd": "0",
    }
    reserved = {
        "index": 1,
        "provider": "fixture",
        "model": "fixture",
        "outcome": "reserved",
        "request_sha256": "1" * 64,
        "reservation_usd": "0.05",
    }
    completed = {**reserved, "outcome": "completed", "actual_cost_usd": "0.04"}
    if failure == "uncertain":
        completed["outcome"] = "uncertain"
    elif failure == "cost":
        metadata["spent_usd"] = "0.01"
    elif failure == "model":
        completed["model"] = "other"
    rows = [reserved, completed]
    if failure == "duplicate":
        rows.append(completed)
    path = tmp_path / "provider-costs.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    (tmp_path / "observations.json").write_text(
        json.dumps(
            [
                {
                    "provider_calls": 2 if failure == "observed_calls" else 1,
                    "cost_usd": "0.01" if failure == "observed_cost" else "0.04",
                }
            ]
        )
    )
    (tmp_path / "ordinary-observations.jsonl").write_text("")
    if failure:
        with pytest.raises(ValueError):
            verify_cost_journal(tmp_path, metadata)
    else:
        verify_cost_journal(tmp_path, metadata)
