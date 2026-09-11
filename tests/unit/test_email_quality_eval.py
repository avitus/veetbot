"""Synthetic labels verify the scorer, never the product's owner usefulness."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.evals.email_quality import (
    EmailQualityCorpus,
    EmailQualityReport,
    score_email_quality,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
CATEGORIES = [
    "regular_reply_partner",
    "close_collaborator",
    "portfolio_founder",
    "prospective_investment_founder",
    "portfolio_board_member",
    "venture_investor",
    "personal",
    "new_relevant_sender",
    "bulk",
    "low_value",
]


def fixture() -> dict[str, Any]:
    threads = []
    for index in range(200):
        threads.append(
            {
                "id": UUID(int=index + 1),
                "conversation_id": UUID(int=index + 1001),
                "account": f"account_{index % 2 + 1}",
                "split": "training" if index < 100 else "holdout",
                "source_at": START if index < 100 else START + timedelta(days=2),
                "relationship": CATEGORIES[index % 10],
                "important": index % 10 < 5,
                "content_kind": "actionable" if index % 2 else "informational",
                "needs_reply": index % 10 < 3,
                "unambiguous": True,
                "fully_supported": True,
            }
        )
    snapshots = []
    for index in range(30):
        pool = [item["id"] for item in threads[100 + (index % 10) * 10 : 110 + (index % 10) * 10]]
        snapshots.append(
            {
                "id": UUID(int=3000 + index),
                "observed_at": START + timedelta(days=3 + index),
                "profile_evidence_through": START + timedelta(days=1),
                "profile_thread_ids": [threads[0]["id"]],
                "complete_candidate_pool": True,
                "independent_snapshot": True,
                "candidate_ids": pool,
                "ranked_ids": list(pool),
                "displayed_ids": pool[:5],
                "baselines": {
                    name: list(pool[1:] + pool[:1])
                    for name in ("newest_first", "gmail_important", "without_personalization")
                },
                "auto_drafted_ids": pool[:3],
            }
        )
    return {
        "origin": "owner_private",
        "frozen_at": START,
        "tuning_started_at": START + timedelta(days=1),
        "holdout_started_at": START + timedelta(days=2),
        "policy": {
            "ranker_version": "priority@1",
            "style_version": "style@1",
            "semantic_policy_version": "email-semantic@1",
            "provider": "fake",
            "model": "fixture",
            "build_ref": "a" * 40,
            "implementation_sha256": "b" * 64,
        },
        "threads": threads,
        "snapshots": snapshots,
        "style": [
            {
                "id": UUID(int=5000 + index),
                "thread_id": threads[100 + index]["id"],
                "blind_paired": True,
                "preferred": "personalized",
                "substantial_rewrite": False,
                "factually_faithful": True,
            }
            for index in range(30)
        ],
        "memory": [
            {
                "thread_id": threads[100 + index]["id"],
                "expected_useful": 10,
                "emitted": 10,
                "supported_useful": 10,
                "useful_beyond_excerpt": 1,
            }
            for index in range(30)
        ],
        "existing_memory_benchmark_passed": True,
    }


def score(data: dict[str, Any]) -> EmailQualityReport:
    return score_email_quality(EmailQualityCorpus.model_validate(data))


def test_email_quality_rejects_future_profile_and_cross_split_conversations() -> None:
    data = fixture()
    data["threads"][100]["conversation_id"] = data["threads"][0]["conversation_id"]
    with pytest.raises(ValidationError, match="conversation"):
        EmailQualityCorpus.model_validate(data)
    data = fixture()
    data["snapshots"][0]["profile_thread_ids"] = [data["threads"][100]["id"]]
    with pytest.raises(ValidationError, match="training"):
        EmailQualityCorpus.model_validate(data)


def test_email_quality_reports_pending_without_private_owner_evidence() -> None:
    assert score_email_quality(None).status == "pending"
    data = fixture()
    data["origin"] = "synthetic"
    assert score(data).status == "pending"
    assert score(data).checks["owner_evidence"].status == "pending"


def test_email_quality_scores_thresholds_counts_baselines_and_uncertainty() -> None:
    result = score(fixture())
    assert result.status == "passed"
    assert result.metrics["priority"]["precision"]["value"] == 1
    assert result.metrics["priority"]["displayed_appearances"] == 150
    assert result.metrics["priority"]["independent_displayed_threads"] == 50
    assert result.metrics["priority"]["precision"]["interval_95"] == [1, 1]
    assert result.metrics["draft"]["emitted_independent_threads"] == 30
    assert result.metrics["personalization"]["strongest_baseline_precision"] == 0.8
    assert result.corpus_sha256 and len(result.corpus_sha256) == 64


def test_email_quality_fails_weak_account_even_when_aggregate_passes() -> None:
    data = fixture()
    for item in data["threads"][100:]:
        if item["id"].int % 10 == 2:
            item["important"] = False
    result = score(data)
    assert result.checks["priority_precision"].status == "failed"
    assert result.metrics["priority"]["by_account"]["account_2"]["value"] == 0.5


@pytest.mark.parametrize("change", ["missing", "duplicate", "unblind"])
def test_email_quality_rejects_missing_candidates_duplicate_ranks_and_unblind_style(
    change: str,
) -> None:
    data = fixture()
    if change == "missing":
        data["snapshots"][0]["ranked_ids"].pop()
    elif change == "duplicate":
        data["snapshots"][0]["ranked_ids"][0] = data["snapshots"][0]["ranked_ids"][1]
    else:
        data["style"][0]["blind_paired"] = False
    with pytest.raises(ValidationError):
        EmailQualityCorpus.model_validate(data)


def test_email_quality_blocks_fabrications_and_missing_memory_benchmark() -> None:
    data = fixture()
    data["style"][0]["fabricated_commitment"] = True
    result = score(data)
    assert result.checks["faithfulness"].status == "failed"
    data = fixture()
    data["existing_memory_benchmark_passed"] = None
    assert score(data).checks["semantic_memory"].status == "pending"


def test_email_quality_abstentions_cannot_pass_draft_coverage() -> None:
    data = fixture()
    for snapshot in data["snapshots"]:
        snapshot["auto_drafted_ids"] = []
    result = score(data)
    assert result.checks["draft_relevance"].status != "passed"
    assert result.metrics["draft"]["abstentions"] == 90


def test_email_quality_duplicate_conversation_cannot_leak_through_another_account() -> None:
    data = fixture()
    data["threads"][101]["conversation_id"] = data["threads"][0]["conversation_id"]
    with pytest.raises(ValidationError, match="conversation"):
        EmailQualityCorpus.model_validate(data)


def test_email_quality_blocks_unsupported_auto_drafts_and_known_semantic_violations() -> None:
    data = fixture()
    data["threads"][100]["fully_supported"] = False
    assert score(data).checks["draft_relevance"].status == "failed"
    data = fixture()
    data["existing_memory_benchmark_passed"] = None
    data["memory"][0]["authority_violations"] = 1
    assert score(data).checks["semantic_memory"].status == "failed"


def test_email_quality_uses_declared_ceiling_rule_without_coverage_regression() -> None:
    data = fixture()
    for snapshot in data["snapshots"]:
        snapshot["baselines"] = {
            name: list(snapshot["ranked_ids"])
            for name in ("newest_first", "gmail_important", "without_personalization")
        }
    result = score(data)
    assert result.checks["personalization"].status == "passed"
    assert result.metrics["personalization"]["ceiling_rule_applied"] is True
    assert result.metrics["personalization"]["precision_lift"] == 0


@pytest.mark.parametrize("field", ["preferred", "substantial_rewrite"])
def test_email_quality_style_threshold_does_not_round_up(field: str) -> None:
    data = fixture()
    for item in data["style"][:10]:
        item[field] = "baseline" if field == "preferred" else True
    assert score(data).checks["style_usefulness"].status == "failed"
    data["style"][9][field] = "personalized" if field == "preferred" else False
    assert score(data).checks["style_usefulness"].status == "passed"


def test_email_quality_shape_rejects_content_fields_and_report_has_no_source_ids() -> None:
    data = fixture()
    data["threads"][0]["body"] = "synthetic unexpected content"
    with pytest.raises(ValidationError, match="Extra inputs"):
        EmailQualityCorpus.model_validate(data)
    data = fixture()
    report = score(data).model_dump_json()
    assert str(data["threads"][0]["id"]) not in report
    assert "thread_id" not in report
