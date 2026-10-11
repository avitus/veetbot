"""Measured three-arm observations and release scoring on the frozen M32 corpus."""

from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field

from agent_core.domain.reconsolidation_release import ReconsolidationQuality
from agent_core.evals.memory_reconsolidation import (
    Observation,
    Score,
    StrictValue,
    load_corpus,
    score_case,
)

Arm = Literal["original_only", "merge_only", "connections"]
ARMS: tuple[Arm, ...] = ("original_only", "merge_only", "connections")


class ComparisonRow(StrictValue):
    arm: Arm
    repeat: int = Field(ge=1, le=3)
    observation: Observation
    failure: Literal["runtime_failure", "answer_failure", "budget_exhausted"] | None = None
    boundary_failures: int = Field(default=0, ge=0)
    provider_calls: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    elapsed_ms: int = Field(default=0, ge=0)
    trace_sha256: tuple[str, ...] = ()


def quality_failures(root: Path, rows: tuple[ComparisonRow, ...]) -> tuple[str, ...]:
    failures, _ = score_comparison(root, rows)
    return failures


def score_comparison(
    root: Path,
    rows: tuple[ComparisonRow, ...],
    *,
    hypothesis_matches: tuple[int, ...] | None = None,
) -> tuple[tuple[str, ...], tuple[ReconsolidationQuality, ...]]:
    corpus = load_corpus(root)
    cases = {case.id: case for case in corpus.cases}
    expected = {(arm, repeat, key) for arm in ARMS for repeat in (1, 2, 3) for key in cases}
    found = {(r.arm, r.repeat, r.observation.case_id) for r in rows}
    if found != expected or len(rows) != len(expected):
        return ("incomplete_or_duplicate_census",), ()
    if hypothesis_matches is not None and (
        len(hypothesis_matches) != len(rows)
        or any(
            not 0
            <= matches
            <= min(len(row.observation.hypotheses), len(cases[row.observation.case_id].hypotheses))
            for row, matches in zip(rows, hypothesis_matches, strict=True)
        )
    ):
        raise ValueError("invalid reviewed match counts")
    failures = []
    if any(r.failure is not None for r in rows):
        failures.append("failed_cases")
    if any(r.boundary_failures for r in rows):
        failures.append("boundary_failures")
    if any(
        (r.arm == "original_only" and (r.observation.merges or r.observation.hypotheses))
        or (r.arm == "merge_only" and r.observation.hypotheses)
        for r in rows
    ):
        failures.append("invalid_control_arm")
    qualities = []
    for split in ("development", "holdout"):
        scores = {}
        for arm in ARMS:
            counts = []
            for index, row in enumerate(rows):
                if row.arm != arm or cases[row.observation.case_id].split != split:
                    continue
                score = score_case(cases[row.observation.case_id], row.observation)
                if hypothesis_matches is not None:
                    score = replace(score, hypothesis_matches=hypothesis_matches[index])
                counts.append(score)
            scores[arm] = Score(
                **{name: sum(asdict(c)[name] for c in counts) for name in asdict(Score())}
            )
            if any(
                (
                    scores[arm].false_merge_pairs,
                    scores[arm].lost_facts,
                    scores[arm].invalid_observations,
                )
            ):
                failures.append(f"{split}:{arm}:unsafe_observation")
        original, merged, connected = (scores[arm] for arm in ARMS)

        def ratio(n: int, d: int) -> float:
            return n / d if d else 0.0

        duplicate = min(
            ratio(score.covered_duplicate_sets, score.duplicate_sets)
            for score in (merged, connected)
        )
        precision = ratio(connected.hypothesis_matches, connected.hypothesis_outputs)
        recall = ratio(connected.hypothesis_matches, connected.hypothesis_labels)
        lift = ratio(connected.covered_probes, connected.probes) - ratio(
            original.covered_probes, original.probes
        )
        regressed = merged.covered_probes < original.covered_probes
        if duplicate < 0.8:
            failures.append(f"{split}:duplicate_coverage")
        if precision < 0.8 or recall < 0.6:
            failures.append(f"{split}:hypothesis_quality")
        if lift < 0.1:
            failures.append(f"{split}:answer_coverage_lift")
        if regressed:
            failures.append(f"{split}:merge_answer_regression")
        if not failures:
            qualities.append(
                ReconsolidationQuality(
                    split=split,
                    false_merges=0,
                    lost_facts=0,
                    boundary_failures=0,
                    failures=0,
                    duplicate_coverage=duplicate,
                    hypothesis_precision=precision,
                    hypothesis_recall=recall,
                    answer_coverage_lift=lift,
                    merge_answer_regressions=0,
                )
            )
    return tuple(failures), tuple(qualities) if not failures else ()
