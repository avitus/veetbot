"""Offline paired M26 quality checks required by Email People activation.

Inputs contain the same opaque owner labels and separate observed outputs.
No provider or mailbox is accessed, and synthetic inputs cannot pass the gate.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from agent_core.evals.email_quality import EmailQualityCorpus, EmailSnapshot, score_email_quality


def _rate(hits: int, total: int) -> Fraction:
    return Fraction(hits, total) if total else Fraction(1)


def _snapshot_quality(snapshot: EmailSnapshot, corpus: EmailQualityCorpus) -> tuple[Fraction, ...]:
    labels = {row.id: row for row in corpus.threads}
    important = {key for key in snapshot.candidate_ids if labels[key].important}
    drafted = set(snapshot.auto_drafted_ids)
    eligible = [
        key
        for key in snapshot.ranked_ids
        if labels[key].needs_reply and labels[key].unambiguous and labels[key].fully_supported
    ][:3]
    return (
        _rate(
            sum(labels[key].important for key in snapshot.displayed_ids),
            len(snapshot.displayed_ids),
        ),
        _rate(len(important & set(snapshot.ranked_ids[:10])), len(important)),
        _rate(sum(labels[key].needs_reply for key in drafted), len(drafted)),
        _rate(len(drafted & set(eligible)), len(eligible)),
    )


def compare_ordinary_email(
    baseline_data: dict[str, Any],
    candidate_data: dict[str, Any],
) -> dict[str, Any]:
    baseline = EmailQualityCorpus.model_validate(baseline_data)
    candidate = EmailQualityCorpus.model_validate(candidate_data)
    if (
        baseline.policy.semantic_policy_version != "email-semantic@1"
        or candidate.policy.semantic_policy_version != "email-semantic@2"
        or baseline.policy.model_dump(exclude={"semantic_policy_version", "implementation_sha256"})
        != candidate.policy.model_dump(exclude={"semantic_policy_version", "implementation_sha256"})
        or baseline.model_dump(
            exclude={
                "policy",
                "threads",
                "snapshots",
                "style",
                "memory",
                "existing_memory_benchmark_passed",
            }
        )
        != candidate.model_dump(
            exclude={
                "policy",
                "threads",
                "snapshots",
                "style",
                "memory",
                "existing_memory_benchmark_passed",
            }
        )
        or {row.id: row for row in baseline.threads} != {row.id: row for row in candidate.threads}
    ):
        raise ValueError(
            "ordinary Email comparisons require the same frozen labels, release and model"
        )
    before = {row.id: row for row in baseline.snapshots}
    after = {row.id: row for row in candidate.snapshots}
    outputs = {"ranked_ids", "displayed_ids", "auto_drafted_ids"}
    if set(before) != set(after) or any(
        row.model_dump(exclude=outputs) != after[key].model_dump(exclude=outputs)
        for key, row in before.items()
    ):
        raise ValueError("ordinary Email snapshots must pair the same evidence and candidate pools")
    regressions = sum(
        any(
            new < old
            for old, new in zip(
                _snapshot_quality(row, baseline),
                _snapshot_quality(after[key], candidate),
                strict=True,
            )
        )
        for key, row in before.items()
    )
    prior_style = {row.id: row for row in baseline.style}
    next_style = {row.id: row for row in candidate.style}
    if set(prior_style) != set(next_style) or any(
        row.thread_id != next_style[key].thread_id for key, row in prior_style.items()
    ):
        raise ValueError("ordinary Email style judgments require the same blind pairs")
    preference = {"baseline": 0, "tie": 1, "personalized": 2}
    for key, row in prior_style.items():
        current = next_style[key]
        regressions += int(
            preference[current.preferred] < preference[row.preferred]
            or (not row.substantial_rewrite and current.substantial_rewrite)
            or (row.factually_faithful and not current.factually_faithful)
            or (not row.fabricated_commitment and current.fabricated_commitment)
            or (not row.fabricated_recipient and current.fabricated_recipient)
        )
    prior_memory = {row.thread_id: row for row in baseline.memory}
    next_memory = {row.thread_id: row for row in candidate.memory}
    if set(prior_memory) != set(next_memory) or any(
        row.expected_useful != next_memory[key].expected_useful for key, row in prior_memory.items()
    ):
        raise ValueError("ordinary Email semantic judgments require the same useful-fact labels")
    for key, old in prior_memory.items():
        new = next_memory[key]
        regressions += int(
            new.supported_useful < old.supported_useful
            or _rate(new.supported_useful, new.emitted) < _rate(old.supported_useful, old.emitted)
            or new.useful_beyond_excerpt < old.useful_beyond_excerpt
            or new.attribution_violations > old.attribution_violations
            or new.authority_violations > old.authority_violations
        )
    previous = score_email_quality(baseline)
    current_report = score_email_quality(candidate)
    status = (
        "failed"
        if regressions or "failed" in {previous.status, current_report.status}
        else "pending"
        if "pending" in {previous.status, current_report.status}
        else "passed"
    )
    return {
        "status": status,
        "baseline_corpus_sha256": previous.corpus_sha256,
        "candidate_corpus_sha256": current_report.corpus_sha256,
        "baseline_policy": baseline.policy.model_dump(mode="json"),
        "candidate_policy": candidate.policy.model_dump(mode="json"),
        "threads": len(baseline.threads),
        "snapshots": len(before),
        "ordinary_email_regressions": regressions,
        "baseline_checks": {key: value.status for key, value in previous.checks.items()},
        "candidate_checks": {key: value.status for key, value in current_report.checks.items()},
    }
