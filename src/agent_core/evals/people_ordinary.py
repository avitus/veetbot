"""Measure the People candidate against the unchanged ordinary-memory corpus."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_core.config import Settings
from agent_core.domain.messages import ResolvedModel
from agent_core.evals.memory_distillation import (
    DistillationCaseResult,
    PolicyVersion,
    _evaluate_case,
    _policy_metrics,
    load_distillation_corpus,
    load_distillation_holdout,
)
from agent_core.evals.people_execution import BorrowedProvider, BudgetedProvider

ORDINARY_POLICIES: tuple[PolicyVersion, ...] = ("formation@9", "formation@11")


def summarize_ordinary(results: list[DistillationCaseResult]) -> dict[str, Any]:
    """Compare the same strict metrics, retaining every failed repeat."""
    summaries = {policy: _policy_metrics(policy, results) for policy in ORDINARY_POLICIES}
    baseline, candidate = (summaries[policy] for policy in ORDINARY_POLICIES)
    metrics = (
        "direct_must_form_recall",
        "hypothesis_must_form_recall",
        "benign_precision",
        "evidence_disposition_precision",
    )
    regressions = [name for name in metrics if getattr(candidate, name) < getattr(baseline, name)]
    return {
        "baseline": baseline.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "regressions": regressions,
        "ordinary_memory_regressions": len(regressions),
        "formation_call_mismatches": sum(
            arm.provider_calls != arm.expected_provider_calls
            for result in results
            for arm in result.arms.values()
        ),
        "boundary_failures": sum(
            result.arms["formation@11"].score.boundary_failures for result in results
        ),
        "cases": len(results),
    }


async def run_ordinary_comparison(
    root: Path,
    *,
    output: Path,
    settings: Settings,
    model_policy: str,
    policy_profile: str,
    provider: BudgetedProvider,
    resolved: ResolvedModel,
    repeats: int,
) -> dict[str, object]:
    development, development_digest = load_distillation_corpus(root)
    holdout, holdout_digest = load_distillation_holdout(root)
    reports = []
    with (output / "ordinary-observations.jsonl").open("x") as stream:
        for repeat in range(repeats):
            for split, corpus in (("development", development), ("holdout", holdout)):
                results = []
                for index, case in enumerate(corpus.cases):
                    arms = {}
                    policies = (
                        ORDINARY_POLICIES
                        if (index + repeat) % 2
                        else tuple(reversed(ORDINARY_POLICIES))
                    )
                    for policy in policies:
                        arm = await _evaluate_case(
                            settings,
                            case,
                            model_policy=model_policy,
                            policy_profile=policy_profile,
                            policy_version=policy,
                            seeds=corpus.seeds_for(case),
                            model_provider_overrides={
                                resolved.provider: BorrowedProvider(provider)
                            },
                        )
                        if provider.budget.failed or provider.budget.held:
                            raise ValueError(
                                "ordinary comparison has unresolved provider accounting"
                            )
                        if arm.identity is None or arm.identity[:2] != (
                            resolved.provider,
                            resolved.model,
                        ):
                            raise ValueError("ordinary comparison changed the provider/model tuple")
                        arms[policy] = arm
                        stream.write(
                            json.dumps(
                                {
                                    "repeat": repeat,
                                    "split": split,
                                    "case_id": case.id,
                                    "arm": arm.model_dump(mode="json"),
                                }
                            )
                            + "\n"
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    results.append(
                        DistillationCaseResult(
                            case_id=case.id,
                            label=case.label,
                            scenario=case.scenario,
                            arms=arms,
                            run_index=repeat,
                        )
                    )
                reports.append({"repeat": repeat, "split": split, **summarize_ordinary(results)})
    return {
        "corpus_sha256": development_digest,
        "holdout_sha256": holdout_digest,
        "reports": reports,
        "activation_evidence": False,
    }
