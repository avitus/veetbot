"""Freeze answer controls before new arms; publish only a complete measured tuple."""

import json
import subprocess
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field

from agent_core.domain.messages import ResolvedModel
from agent_core.domain.reconsolidation_release import ReconsolidationEvidence
from agent_core.evals.memory_reconsolidation import Score, StrictValue, load_corpus, score_case
from agent_core.evals.memory_reconsolidation_comparison import ARMS, ComparisonRow, score_comparison
from agent_core.evals.memory_reconsolidation_control import CONTROL_AT, ControlBudget, runtime_case
from agent_core.evals.memory_reconsolidation_runtime import (
    MeasuredProvider,
    collect_comparison_case,
)
from agent_core.memory.reconsolidation_evidence import (
    file_digest,
    implementation_digest,
    model_digest,
    scorer_digest,
)
from agent_core.memory.reconsolidation_policy import privacy_digest
from agent_core.memory.retrieval import RETRIEVAL_POLICY_VERSION
from agent_core.ports.models import ModelProvider


class ComparisonRecording(StrictValue):
    schema_version: Literal[1] = 1
    purpose: Literal["answer_control", "three_arm_comparison"]
    activation_evidence: Literal[False] = False
    code_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    working_tree_dirty: bool
    implementation_sha256: str
    corpus_sha256: str
    scorer_sha256: str
    provider: str
    model: str
    reasoning_effort: Literal["provider-default"] = "provider-default"
    model_configuration_sha256: str
    privacy_sha256: str
    retrieval_policy: str = RETRIEVAL_POLICY_VERSION
    clock_at: str = CONTROL_AT.isoformat()
    budget: ControlBudget = ControlBudget()
    original_control_sha256: str | None = None
    rows: tuple[ComparisonRow, ...]
    counts: dict[str, dict[str, int]]
    failures: tuple[str, ...]
    provider_calls: int
    cost_usd: Decimal
    unknown_usage_calls: int
    review_contract_sha256: str | None = None


def counts(root: Path, rows: tuple[ComparisonRow, ...]) -> dict[str, dict[str, int]]:
    cases = {case.id: case for case in load_corpus(root).cases}
    result = {}
    for split in ("development", "holdout"):
        for arm in ARMS:
            scores = [
                score_case(cases[r.observation.case_id], r.observation)
                for r in rows
                if r.arm == arm and cases[r.observation.case_id].split == split
            ]
            result[f"{split}:{arm}"] = {
                key: sum(asdict(score)[key] for score in scores) for key in asdict(Score())
            }
    return result


def recording_failures(
    root: Path,
    rows: tuple[ComparisonRow, ...],
    *,
    purpose: Literal["answer_control", "three_arm_comparison"],
    unknown_usage_calls: int,
) -> tuple[str, ...]:
    cases = {case.id: case for case in load_corpus(root).cases}
    arms = ("original_only",) if purpose == "answer_control" else ARMS
    expected = {(arm, repeat, key) for arm in arms for repeat in (1, 2, 3) for key in cases}
    if {(r.arm, r.repeat, r.observation.case_id) for r in rows} != expected or len(rows) != len(
        expected
    ):
        raise ValueError("recording requires a complete unique census")
    failures, _ = score_comparison(root, rows)
    if purpose == "answer_control":
        failures = ("failed_cases",) if any(r.failure for r in rows) else ()
        if any(r.observation.merges or r.observation.hypotheses for r in rows):
            failures += ("invalid_control_arm",)
    if any(
        r.failure is None
        and (
            r.provider_calls < len(cases[r.observation.case_id].probes)
            or r.cost_usd <= 0
            or len(r.trace_sha256) != len(cases[r.observation.case_id].probes)
            or any(
                len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                for digest in r.trace_sha256
            )
            or {answer.probe_id for answer in r.observation.answers}
            != {probe.id for probe in cases[r.observation.case_id].probes}
        )
        for r in rows
    ):
        failures += ("unmeasured_rows",)
    if unknown_usage_calls:
        failures += ("unknown_usage",)
    return failures


def record(
    root: Path,
    rows: tuple[ComparisonRow, ...],
    model: ResolvedModel,
    *,
    purpose: Literal["answer_control", "three_arm_comparison"],
    unknown_usage_calls: int,
    original_control_sha256: str | None = None,
    review_contract_sha256: str | None = None,
) -> ComparisonRecording:
    failures = recording_failures(
        root, rows, purpose=purpose, unknown_usage_calls=unknown_usage_calls
    )
    return ComparisonRecording(
        purpose=purpose,
        code_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        working_tree_dirty=bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=root)
        ),
        implementation_sha256=implementation_digest(root),
        corpus_sha256=file_digest(root / "evals/capability/memory-reconsolidation.v1.json"),
        scorer_sha256=scorer_digest(root),
        provider=model.provider,
        model=model.model,
        model_configuration_sha256=model_digest(model),
        privacy_sha256=privacy_digest(model.provider),
        original_control_sha256=original_control_sha256,
        review_contract_sha256=review_contract_sha256,
        rows=rows,
        counts=counts(root, rows),
        failures=failures,
        provider_calls=sum(r.provider_calls for r in rows),
        cost_usd=sum((r.cost_usd for r in rows), Decimal(0)),
        unknown_usage_calls=unknown_usage_calls,
    )


def write_exclusive(path: Path, value: ComparisonRecording) -> None:
    with path.open("x") as handle:
        handle.write(value.model_dump_json(indent=2) + "\n")


async def run_comparison(
    root: Path,
    control_path: Path,
    report_path: Path,
    model: ResolvedModel,
    provider: ModelProvider,
    *,
    maximum_usd: Decimal = Decimal("50"),
    review_contract: Path | None = None,
) -> ComparisonRecording:
    if control_path.exists() or report_path.exists():
        raise ValueError("recordings must use new paths")
    review_digest = None
    if review_contract is not None:
        from agent_core.evals.memory_reconsolidation_review import load_review_contract

        review_digest = load_review_contract(root, review_contract)
    corpus = load_corpus(root)
    implementation = implementation_digest(root)
    measured = MeasuredProvider(provider, maximum_usd=maximum_usd)
    rows: list[ComparisonRow] = []
    # Baseline is completed and frozen before any candidate proposal is requested.
    for arm in ARMS:
        for repeat in (1, 2, 3):
            for case in corpus.cases:
                row = await collect_comparison_case(
                    runtime_case(case), arm, repeat, model=model, provider=measured
                )
                rows.append(row)
                print(
                    json.dumps(
                        {
                            "arm": arm,
                            "repeat": repeat,
                            "case": case.id,
                            "failure": row.failure,
                            "calls": measured.calls,
                            "cost_usd": str(measured.cost),
                        }
                    ),
                    flush=True,
                )
        if review_contract is not None and file_digest(review_contract) != review_digest:
            raise ValueError("review contract changed during measurement")
        if implementation_digest(root) != implementation:
            raise ValueError("implementation changed during measurement")
        if arm == "original_only":
            control = record(
                root,
                tuple(rows),
                model,
                purpose="answer_control",
                unknown_usage_calls=measured.unknown_calls,
                review_contract_sha256=review_digest,
            )
            write_exclusive(control_path, control)
    report = record(
        root,
        tuple(rows),
        model,
        purpose="three_arm_comparison",
        unknown_usage_calls=measured.unknown_calls,
        original_control_sha256=file_digest(control_path),
        review_contract_sha256=review_digest,
    )
    write_exclusive(report_path, report)
    return report


def publish(
    root: Path,
    report_path: Path,
    control_path: Path,
    output: Path,
    model: ResolvedModel,
    *,
    formation_policy: str,
    upstream_path: Path,
    recall_path: Path,
) -> ReconsolidationEvidence:
    from agent_core.config import load_memory_release_evidence, load_people_formation_evidence

    upstream = (
        load_people_formation_evidence(upstream_path)
        if formation_policy == "formation@11"
        else load_memory_release_evidence(upstream_path)
    )
    if (upstream.provider, upstream.model, upstream.formation_policy_version) != (
        model.provider,
        model.model,
        formation_policy,
    ):
        raise ValueError("upstream evidence does not match the active model and policy")
    from agent_core.evals.memory_benchmark_live import MemoryBenchmarkEvidence

    recall = MemoryBenchmarkEvidence.model_validate_json(recall_path.read_bytes())
    report = ComparisonRecording.model_validate_json(report_path.read_bytes())
    if (
        recall.build_ref,
        recall.provider,
        recall.model,
        recall.formation_policy_version,
        recall.retrieval_policy_version,
    ) != (
        report.code_commit,
        model.provider,
        model.model,
        formation_policy,
        RETRIEVAL_POLICY_VERSION,
    ) or recall.corpus_sha256 != file_digest(root / "evals/capability/memory-benchmark.v1.json"):
        raise ValueError("recall floor evidence does not match the release tuple")
    control = ComparisonRecording.model_validate_json(control_path.read_bytes())
    failures, quality = score_comparison(root, report.rows)
    if failures or report.failures or report.working_tree_dirty or report.unknown_usage_calls:
        raise ValueError("comparison cannot activate reconsolidation")
    expected = record(
        root,
        report.rows,
        model,
        purpose="three_arm_comparison",
        unknown_usage_calls=0,
        original_control_sha256=file_digest(control_path),
    )
    if report != expected or control.rows != tuple(
        r for r in report.rows if r.arm == "original_only"
    ):
        raise ValueError("comparison metadata or frozen original control changed")
    expected_control = record(
        root, control.rows, model, purpose="answer_control", unknown_usage_calls=0
    )
    if control != expected_control or control.failures:
        raise ValueError("original control metadata or measurements are invalid")
    evidence = ReconsolidationEvidence(
        build_ref=report.code_commit,
        implementation_sha256=report.implementation_sha256,
        corpus_sha256=report.corpus_sha256,
        scorer_sha256=report.scorer_sha256,
        report_sha256=file_digest(report_path),
        recall_report_sha256=file_digest(recall_path),
        recall_build_ref=recall.build_ref,
        recall_corpus_sha256=recall.corpus_sha256,
        provider=report.provider,
        model=report.model,
        model_configuration_sha256=report.model_configuration_sha256,
        privacy_sha256=report.privacy_sha256,
        formation_policy=formation_policy,
        retrieval_policy=report.retrieval_policy,
        upstream_build_ref=upstream.build_ref,
        upstream_corpus_sha256=upstream.corpus_sha256,
        quality=quality,
    )
    with output.open("x") as handle:
        handle.write(evidence.model_dump_json(indent=2) + "\n")
    return evidence


async def run_configured_comparison(
    root: Path,
    control_path: Path,
    report_path: Path,
    *,
    maximum_usd: Decimal,
    review_contract: Path | None = None,
) -> ComparisonRecording:
    """Use the configured formation model and credentials with fixed evaluation inputs."""
    import importlib
    import os

    from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
    from agent_core.config import PACKAGE_ROOT, load_config_document, load_settings
    from agent_core.memory.profiles import MemoryProfiles
    from agent_core.model.registry import ProviderRegistry, StaticModelRouter

    environment = dict(os.environ)
    environment.setdefault("DATABASE_URL", "postgresql+asyncpg://127.0.0.1:1/unused")
    environment.setdefault("DEPLOYMENT_MODE", "development")
    environment.setdefault("SANDBOX_MECHANISM", "fake")
    settings = load_settings(environment)
    profile = MemoryProfiles.from_document(load_config_document(settings, "memory/profiles.yaml"))
    if profile.formation.reasoning_effort is not None:
        raise ValueError("comparison requires the pinned provider-default effort")
    registry = ProviderRegistry.load(
        PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS, overlay_root=settings.config_dir
    )
    bootstrap = importlib.import_module("agent_core.bootstrap")
    model = await StaticModelRouter(registry, bootstrap.system_clock()).resolve(
        profile.formation.model_policy, tenant_id="evaluation"
    )
    providers = bootstrap._provider_adapters(settings, registry)
    try:
        return await run_comparison(
            root,
            control_path,
            report_path,
            model,
            providers[model.provider],
            maximum_usd=maximum_usd,
            review_contract=review_contract,
        )
    finally:
        for provider in providers.values():
            await provider.close()
