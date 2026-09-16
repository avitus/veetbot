"""Compile Email People evidence only after complete comparisons and owner labels."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent_core.domain.email_people_evidence import (
    EmailPeopleEvidence,
    EmailPeopleMetrics,
    OrdinaryEmailEvidence,
)
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.people_evidence import PeopleFormationEvidence
from agent_core.evals.email_people import load_email_corpora, score_observations
from agent_core.evals.email_people_ordinary import compare_ordinary_email
from agent_core.evals.memory_distillation import require_committed_tree
from agent_core.evals.people_release import verify_cost_journal


def run_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in ("run.json", "observations.json", "provider-costs.jsonl"):
        digest.update(name.encode())
        digest.update(hashlib.sha256((directory / name).read_bytes()).digest())
    return digest.hexdigest()


def assemble_email_evidence(
    metadata: dict[str, Any],
    scores: dict[str, Any],
    ordinary: dict[str, Any],
    people: PeopleFormationEvidence,
    *,
    run_sha256: str,
    evaluated_at: datetime,
) -> EmailPeopleEvidence:
    repeats = metadata["repeats"]
    if (
        metadata["state"] != "completed"
        or metadata["review_status"] != ["reviewed", "reviewed"]
        or scores["review_status"] != ["reviewed", "reviewed"]
        or Decimal(metadata["reserved_usd"]) != 0
        or scores["repeats"] != repeats
        or scores["corpus_sha256"] != metadata["corpus_sha256"]
        or ordinary["status"] != "passed"
        or ordinary["ordinary_email_regressions"]
        or any(value != "passed" for value in ordinary["baseline_checks"].values())
        or any(value != "passed" for value in ordinary["candidate_checks"].values())
    ):
        raise ValueError(
            "Email publication requires reviewed repeats and passing paired ordinary Email quality"
        )
    for policy in (ordinary["baseline_policy"], ordinary["candidate_policy"]):
        if any(policy[key] != metadata[key] for key in ("provider", "model", "build_ref")):
            raise ValueError("ordinary Email evidence belongs to another release or model")
    if ordinary["candidate_policy"]["implementation_sha256"] != metadata["implementation_sha256"]:
        raise ValueError("ordinary Email candidate implementation changed")
    if any(
        getattr(people, key) != metadata[key]
        for key in (
            "provider",
            "model",
            "build_ref",
            "model_policy",
            "policy_profile",
            "policy_version",
            "reasoning_configuration",
        )
    ):
        raise ValueError("Email and People evaluations must use the same release and model policy")
    expected = {
        (split, repeat) for split in ("development", "holdout") for repeat in range(repeats)
    }
    rows = [row for row in scores["reports"] if row["policy"] == "email-semantic@2"]
    indexed = {(row["split"], row["repeat"]): row for row in rows}
    if len(rows) != len(expected) or set(indexed) != expected:
        raise ValueError("Email publication cannot omit or pool a repeat")
    metrics: dict[str, list[EmailPeopleMetrics]] = {"development": [], "holdout": []}
    for split, repeat in sorted(expected):
        row = indexed[(split, repeat)]
        if row["failed_runs"] or row["assessment_calls"] != row["ordinary_cases"]:
            raise ValueError("Email assessment failures or missing calls prevent publication")
        metrics[split].append(
            EmailPeopleMetrics(
                scenarios=row["cases"],
                supported_claim_precision=row["direct_fact_precision"],
                supported_claim_recall=row["direct_fact_recall"],
                identity_precision=row["identity_precision"],
                resolvable_identity_recall=row["resolvable_identity_recall"],
                attribution_accuracy=row["attribution_accuracy"],
                direction_accuracy=row["direction_accuracy"],
                temporal_accuracy=row["temporal_accuracy"],
                false_merges=row["collision_false_merges"],
                draft_completion_failures=row["draft_completion_failures"],
                authority_failures=row["authority_failures"],
                ordinary_email_regressions=row["reply_decision_regressions"],
                automatic_older_mail_capture=row["automatic_older_mail_capture"],
                extra_provider_calls=row["extra_provider_calls"],
                assessment_calls=row["assessment_calls"],
                cost_usd=row["cost_usd"],
            )
        )
    return EmailPeopleEvidence(
        policy_version="email-semantic@2",
        provider=metadata["provider"],
        model=metadata["model"],
        build_ref=metadata["build_ref"],
        schema_sha256=metadata["schema_sha256"],
        implementation_sha256=metadata["implementation_sha256"],
        corpus_sha256=metadata["corpus_sha256"]["development"],
        holdout_sha256=metadata["corpus_sha256"]["holdout"],
        people=people,
        run_metrics=metrics["development"],
        holdout_metrics=metrics["holdout"],
        run_sha256=run_sha256,
        evaluated_at=evaluated_at,
        ordinary_email=OrdinaryEmailEvidence(
            scorer_version="email-quality@1",
            comparison_version="email-people-ordinary@1",
            baseline_corpus_sha256=ordinary["baseline_corpus_sha256"],
            candidate_corpus_sha256=ordinary["candidate_corpus_sha256"],
            baseline_implementation_sha256=ordinary["baseline_policy"]["implementation_sha256"],
            candidate_implementation_sha256=ordinary["candidate_policy"]["implementation_sha256"],
            threads=ordinary["threads"],
            snapshots=ordinary["snapshots"],
            regressions=0,
            passed=True,
        ),
    )


def publish_evidence(
    root: Path,
    directory: Path,
    people_path: Path,
    baseline_path: Path,
    candidate_path: Path,
    output: Path,
) -> EmailPeopleEvidence:
    from agent_core.config import (
        MEMORY_DISTILLATION_CORPUS_PATH,
        MEMORY_DISTILLATION_HOLDOUT_PATH,
        shipped_corpus_sha256,
        shipped_policy_version,
    )
    from agent_core.memory.email_people_evidence import (
        email_people_implementation_digest,
        email_people_schema_digest,
    )
    from agent_core.memory.email_semantics import semantic_implementation_sha256
    from agent_core.memory.people_evidence import (
        PEOPLE_CORPUS_PATH,
        PEOPLE_HOLDOUT_PATH,
        people_evidence_matches,
    )

    metadata = json.loads((directory / "run.json").read_text())
    evaluated_at = datetime.fromisoformat(metadata["evaluated_at"])
    require_committed_tree(root, metadata["build_ref"])
    development, holdout, digests = load_email_corpora(root)
    if (
        metadata["implementation_sha256"] != email_people_implementation_digest()
        or metadata["schema_sha256"] != email_people_schema_digest()
        or metadata["corpus_sha256"] != digests
        or metadata["policy_version"] != shipped_policy_version(metadata["policy_profile"])
        or [development.review_status, holdout.review_status] != ["reviewed", "reviewed"]
    ):
        raise ValueError("Email comparison no longer matches this reviewed implementation")

    def required_digest(path: Path) -> str:
        value = shipped_corpus_sha256(path)
        if value is None:
            raise ValueError("a required frozen corpus digest is unavailable")
        return value

    people = PeopleFormationEvidence.model_validate_json(people_path.read_text())
    if not people_evidence_matches(
        people,
        ResolvedModel(
            provider=metadata["provider"],
            model=metadata["model"],
            policy_name=metadata["model_policy"],
            resolved_at=evaluated_at,
        ),
        metadata["policy_profile"],
        metadata["policy_version"],
        corpus_sha256=required_digest(PEOPLE_CORPUS_PATH),
        holdout_sha256=required_digest(PEOPLE_HOLDOUT_PATH),
        ordinary_corpus_sha256=required_digest(MEMORY_DISTILLATION_CORPUS_PATH),
        ordinary_holdout_sha256=required_digest(MEMORY_DISTILLATION_HOLDOUT_PATH),
    ):
        raise ValueError("People evidence does not match this release")
    ordinary = compare_ordinary_email(
        json.loads(baseline_path.read_text()), json.loads(candidate_path.read_text())
    )
    if ordinary["baseline_policy"]["implementation_sha256"] != semantic_implementation_sha256():
        raise ValueError("ordinary Email baseline implementation changed")
    observations = json.loads((directory / "observations.json").read_text())
    verify_cost_journal(directory, metadata, observed=[row["people"] for row in observations])
    evidence = assemble_email_evidence(
        metadata,
        score_observations(root, directory / "observations.json"),
        ordinary,
        people,
        run_sha256=run_digest(directory),
        evaluated_at=evaluated_at,
    )
    with output.open("x") as stream:
        stream.write(evidence.model_dump_json(indent=2) + "\n")
    return evidence
