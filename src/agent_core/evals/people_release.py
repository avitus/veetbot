"""Publish version-bound evidence from complete runs and private owner aggregates.

This is an offline compiler. It never supplies owner judgments, runs a provider,
changes configuration, or treats publication as production release approval.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_core.domain.people_evidence import Digest, PeopleFormationEvidence, PeopleQualityMetrics
from agent_core.evals.memory_distillation import (
    DistillationArmResult,
    DistillationCaseResult,
    load_distillation_corpus,
    load_distillation_holdout,
    require_committed_tree,
    score_distillation_case,
)
from agent_core.evals.people import load_corpora, score_observations
from agent_core.evals.people_ordinary import ORDINARY_POLICIES, summarize_ordinary


class OwnerAcceptance(BaseModel):
    """Only permitted, content-free aggregates from an authorized private evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    build_ref: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    run_sha256: Digest
    boundary_failures: Literal[0]
    owner_people_count: int = Field(ge=20, le=30)
    owner_task_count: int = Field(ge=50)
    owner_useful_correct: float = Field(ge=0.9, le=1)
    owner_harmful_mixups: Literal[0]
    evaluated_at: AwareDatetime


def assemble_evidence(
    metadata: dict[str, Any],
    scores: dict[str, Any],
    ordinary: dict[str, Any],
    acceptance: dict[str, Any],
    *,
    labeled_mentions: int,
) -> PeopleFormationEvidence:
    accepted = OwnerAcceptance.model_validate(acceptance)
    repeats = metadata["repeats"]
    if (
        metadata["state"] != "completed"
        or metadata["review_status"] != ["reviewed", "reviewed"]
        or Decimal(metadata["reserved_usd"]) != 0
        or scores["repeats"] != repeats
        or metadata["build_ref"] != accepted.build_ref
    ):
        raise ValueError("publication requires a complete, reviewed, settled comparison")
    expected = {
        (split, repeat) for split in ("development", "holdout") for repeat in range(repeats)
    }
    candidate = [row for row in scores["summaries"] if row["pipeline"] == "full-people"]
    by_key = {(row["split"], row["repeat"]): row for row in candidate}
    ordinary_by_key = {(row["split"], row["repeat"]): row for row in ordinary["reports"]}
    if (
        set(by_key) != expected
        or set(ordinary_by_key) != expected
        or len(candidate) != len(expected)
        or len(ordinary["reports"]) != len(expected)
    ):
        raise ValueError("publication cannot omit, duplicate, or pool a failed repeat")
    metrics: dict[str, list[PeopleQualityMetrics]] = {"development": [], "holdout": []}
    for split, repeat in sorted(expected):
        row, inherited = by_key[(split, repeat)], ordinary_by_key[(split, repeat)]
        if (
            row["formation_failures"]
            or row["formation_call_mismatches"]
            or inherited["formation_call_mismatches"]
        ):
            raise ValueError("formation fallback or call mismatch prevents publication")
        values = {
            key: row[key]
            for key in (
                "identity_precision",
                "resolvable_identity_recall",
                "collision_false_merges",
                "direct_fact_recall",
                "direct_fact_precision",
                "direction_accuracy",
                "attribution_accuracy",
                "temporal_accuracy",
                "task_success",
                "retrieval_evidence_recall",
                "abstentions",
                "unambiguous_decisions",
                "cost_usd",
            )
        }
        values.update(
            provider_calls=row["formation_calls"],
            segments=row["formation_segments"],
            ordinary_memory_regressions=inherited["ordinary_memory_regressions"],
            boundary_failures=inherited["boundary_failures"] + accepted.boundary_failures,
            inherited_direct_recall=inherited["candidate"]["direct_must_form_recall"],
            inherited_hypothesis_recall=inherited["candidate"]["hypothesis_must_form_recall"],
            inherited_benign_precision=inherited["candidate"]["benign_precision"],
            inherited_disposition_precision=inherited["candidate"][
                "evidence_disposition_precision"
            ],
        )
        metrics[split].append(PeopleQualityMetrics.model_validate(values))
    values = {
        key: metadata[key]
        for key in (
            "schema_sha256",
            "implementation_sha256",
            "ordinary_corpus_sha256",
            "ordinary_holdout_sha256",
            "provider",
            "model",
            "model_policy",
            "reasoning_configuration",
            "policy_profile",
            "policy_version",
            "build_ref",
            "repeats",
        )
    }
    values.update(
        schema_version=1,
        formation_policy_version="formation@11",
        extractor_version="people-assisted-v1",
        linker_version="people-linker@1",
        resolver_version="people-resolver@1",
        scorer_version=scores["scorer_version"],
        corpus_sha256=metadata["corpus_sha256"]["development"],
        holdout_sha256=metadata["corpus_sha256"]["holdout"],
        development_scenarios=by_key[("development", 0)]["cases"],
        holdout_scenarios=by_key[("holdout", 0)]["cases"],
        labeled_mentions=labeled_mentions,
        collision_cases=sum(by_key[(split, 0)]["collision_cases"] for split in metrics),
        run_metrics=metrics["development"],
        holdout_metrics=metrics["holdout"],
        comparative_pipelines=("current-memory", "identity-links", "full-people"),
        paired_improvement_ci95_low=scores["paired_improvement_ci95"][0],
        paired_improvement_ci95_high=scores["paired_improvement_ci95"][1],
        **accepted.model_dump(
            exclude={"schema_version", "build_ref", "run_sha256", "boundary_failures"}
        ),
    )
    return PeopleFormationEvidence.model_validate(values)


def rescore_ordinary(root: Path, directory: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    development, development_digest = load_distillation_corpus(root)
    holdout, holdout_digest = load_distillation_holdout(root)
    if (metadata["ordinary_corpus_sha256"], metadata["ordinary_holdout_sha256"]) != (
        development_digest,
        holdout_digest,
    ):
        raise ValueError("ordinary corpus changed after comparison")
    rows = [
        json.loads(line)
        for line in (directory / "ordinary-observations.jsonl").read_text().splitlines()
    ]
    observed = {
        (r["split"], r["repeat"], r["case_id"], r["arm"]["policy_version"]): r for r in rows
    }
    expected = {
        (split, repeat, case.id, policy)
        for split, corpus in (("development", development), ("holdout", holdout))
        for repeat in range(metadata["repeats"])
        for case in corpus.cases
        for policy in ORDINARY_POLICIES
    }
    if len(observed) != len(rows) or set(observed) != expected:
        raise ValueError("ordinary comparison is incomplete or duplicated")
    reports = []
    for split, corpus in (("development", development), ("holdout", holdout)):
        for repeat in range(metadata["repeats"]):
            results = []
            for case in corpus.cases:
                arms = {}
                for policy in ORDINARY_POLICIES:
                    arm = DistillationArmResult.model_validate(
                        observed[(split, repeat, case.id, policy)]["arm"]
                    )
                    if arm.identity != (
                        metadata["provider"],
                        metadata["model"],
                        metadata["policy_version"],
                    ):
                        raise ValueError("ordinary comparison changed provider/model/policy")
                    rescored = score_distillation_case(
                        case,
                        arm.beliefs,
                        evidence_units=arm.score.evidence_units,
                        evidence_units_formed=arm.score.evidence_units_formed,
                    )
                    if rescored != arm.score:
                        raise ValueError("ordinary score differs from its recorded beliefs")
                    arms[policy] = arm
                results.append(
                    DistillationCaseResult(
                        case_id=case.id,
                        label=case.label,
                        scenario=case.scenario,
                        arms=arms,
                        run_index=repeat,
                    )
                )
            reports.append({"split": split, "repeat": repeat, **summarize_ordinary(results)})
    return {"reports": reports}


def run_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in (
        "run.json",
        "observations.json",
        "ordinary-observations.jsonl",
        "provider-costs.jsonl",
    ):
        digest.update(name.encode())
        digest.update(hashlib.sha256((directory / name).read_bytes()).digest())
    return digest.hexdigest()


def verify_cost_journal(
    directory: Path,
    metadata: dict[str, Any],
    *,
    observed: list[dict[str, Any]] | None = None,
) -> None:
    pending: dict[int, dict[str, Any]] = {}
    completed: set[int] = set()
    spent = Decimal(0)
    for line in (directory / "provider-costs.jsonl").read_text().splitlines():
        row = json.loads(line)
        index = row["index"]
        if (row["provider"], row["model"]) != (metadata["provider"], metadata["model"]):
            raise ValueError("cost journal changed the provider/model tuple")
        reserve = Decimal(row["reservation_usd"])
        if not reserve.is_finite() or reserve < 0 or index in completed:
            raise ValueError("invalid or duplicated provider reservation")
        if row["outcome"] == "reserved" and index not in pending:
            pending[index] = row
        elif row["outcome"] == "completed" and index in pending:
            expected = {
                **pending[index],
                "outcome": "completed",
                "actual_cost_usd": row["actual_cost_usd"],
            }
            actual = Decimal(row["actual_cost_usd"])
            if expected != row or not actual.is_finite() or not 0 <= actual <= reserve:
                raise ValueError("provider completion differs from its reservation")
            spent += actual
            completed.add(index)
            del pending[index]
        else:
            raise ValueError("unsettled or invalid provider journal")
    if (
        pending
        or completed != set(range(1, metadata["provider_calls"] + 1))
        or spent != Decimal(metadata["spent_usd"])
        or not spent <= Decimal(metadata["maximum_cost_usd"])
        or Decimal(metadata["reserved_usd"]) != 0
    ):
        raise ValueError("provider journal and run totals disagree")
    if observed is None:
        observed = json.loads((directory / "observations.json").read_text())
        assert observed is not None
        for line in (directory / "ordinary-observations.jsonl").read_text().splitlines():
            arm = json.loads(line)["arm"]
            observed.append(
                {"provider_calls": arm["provider_calls"], "cost_usd": arm["provider_cost_usd"]}
            )
    calls = sum(row["provider_calls"] for row in observed)
    observed_cost = sum((Decimal(row["cost_usd"]) for row in observed), Decimal(0))
    if calls != metadata["provider_calls"] or observed_cost != spent:
        raise ValueError("observed provider calls and costs differ from the settled journal")


def publish_evidence(
    root: Path, directory: Path, acceptance_path: Path, output: Path
) -> PeopleFormationEvidence:
    from agent_core.config import shipped_policy_version
    from agent_core.memory.people_evidence import implementation_digest, schema_digest

    metadata = json.loads((directory / "run.json").read_text())
    require_committed_tree(root, metadata["build_ref"])
    development, holdout, digests = load_corpora(root)
    if (
        metadata["implementation_sha256"] != implementation_digest()
        or metadata["schema_sha256"] != schema_digest()
        or metadata["corpus_sha256"] != digests
        or metadata["policy_version"] != shipped_policy_version(metadata["policy_profile"])
        or [development.review_status, holdout.review_status] != ["reviewed", "reviewed"]
    ):
        raise ValueError("comparison no longer matches this reviewed implementation")
    acceptance = OwnerAcceptance.model_validate_json(acceptance_path.read_text())
    if acceptance.run_sha256 != run_digest(directory):
        raise ValueError("owner acceptance belongs to another comparison run")
    verify_cost_journal(directory, metadata)
    evidence = assemble_evidence(
        metadata,
        score_observations(root, directory / "observations.json"),
        rescore_ordinary(root, directory, metadata),
        acceptance.model_dump(mode="json"),
        labeled_mentions=sum(
            len(case.mentions) for corpus in (development, holdout) for case in corpus.cases
        ),
    )
    with output.open("x") as stream:
        stream.write(evidence.model_dump_json(indent=2) + "\n")
    return evidence
