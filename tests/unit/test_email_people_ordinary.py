"""The People bridge must preserve the complete ordinary Email benchmark."""

from copy import deepcopy
from typing import Any

import pytest

from tests.unit.test_email_quality_eval import fixture


def pair() -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = fixture()
    candidate = deepcopy(baseline)
    candidate["policy"]["semantic_policy_version"] = "email-semantic@2"
    candidate["policy"]["implementation_sha256"] = "c" * 64
    return baseline, candidate


def test_ordinary_email_candidate_retains_the_existing_quality_contract() -> None:
    from agent_core.evals.email_quality import EmailQualityCorpus, score_email_quality

    _baseline, candidate = pair()
    assert score_email_quality(EmailQualityCorpus.model_validate(candidate)).status == "passed"


@pytest.mark.parametrize("change", [None, "priority", "draft", "style", "memory", "synthetic"])
def test_ordinary_email_comparison_preserves_each_paired_case(change: str | None) -> None:
    from agent_core.evals.email_people_ordinary import compare_ordinary_email

    baseline, candidate = pair()
    if change == "priority":
        snapshot = candidate["snapshots"][0]
        snapshot["ranked_ids"][0], snapshot["ranked_ids"][5] = (
            snapshot["ranked_ids"][5],
            snapshot["ranked_ids"][0],
        )
        snapshot["displayed_ids"] = snapshot["ranked_ids"][:5]
    elif change == "draft":
        candidate["snapshots"][0]["auto_drafted_ids"].pop()
    elif change == "style":
        candidate["style"][0]["substantial_rewrite"] = True
    elif change == "memory":
        candidate["memory"][0]["supported_useful"] -= 1
    elif change == "synthetic":
        baseline["origin"] = candidate["origin"] = "synthetic"
    result = compare_ordinary_email(baseline, candidate)
    assert result["status"] == (
        "pending" if change == "synthetic" else "failed" if change else "passed"
    )
    assert result["ordinary_email_regressions"] == (1 if change and change != "synthetic" else 0)
    assert result["threads"] == 200 and result["snapshots"] == 30


@pytest.mark.parametrize(
    "change",
    ["labels", "model", "build", "ranker", "snapshot", "style_pair", "memory_labels", "origin"],
)
def test_ordinary_email_comparison_rejects_unpaired_or_changed_evidence(change: str) -> None:
    from agent_core.evals.email_people_ordinary import compare_ordinary_email

    baseline, candidate = pair()
    if change == "labels":
        candidate["threads"][0]["important"] = False
    elif change in {"model", "ranker", "build"}:
        key = {"model": "model", "ranker": "ranker_version", "build": "build_ref"}[change]
        candidate["policy"][key] = "d" * 40 if change == "build" else "changed"
    elif change == "snapshot":
        candidate["snapshots"].pop()
    elif change == "style_pair":
        candidate["style"].pop()
    elif change == "memory_labels":
        candidate["memory"][0]["expected_useful"] += 1
    elif change == "origin":
        candidate["origin"] = "synthetic"
    with pytest.raises(ValueError):
        compare_ordinary_email(baseline, candidate)
