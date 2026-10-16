"""Run bounded synthetic M33 development measurements; never activation evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.general_recall_release import Channel, PathKind, Pipeline
from agent_core.evals.general_recall_composition import system_clock

if TYPE_CHECKING:
    from agent_core.evals.people_execution import EvaluationBudget


class Experiment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    corpus: Path
    output: Path
    model_policy: str = "balanced"
    maximum_cost_usd: Decimal = Field(default=Decimal("2"), gt=0, le=25, allow_inf_nan=False)
    noise: int = Field(default=1000, ge=0, le=10000)
    deadline_seconds: int = Field(default=600, ge=1, le=3600)
    execution: Literal["runtime", "direct"] = "runtime"
    storage: Literal["memory", "postgres"] = "memory"
    disposable_database: bool = False
    pipeline: Pipeline = "seeded"
    formation_policy: Literal["formation@9", "formation@11"] = "formation@11"
    channel: Channel = "sparse"
    paths: tuple[PathKind, ...] = ("automatic", "deliberate")
    local_configuration: Path | None = None
    model_directory: Path | None = None
    source_recovery: bool = False
    reranking: Literal["baseline", "relevance"] = "baseline"
    query_planning: Literal["baseline", "task_context"] = "baseline"
    search_planning: Literal["scripted", "reader"] = "scripted"
    formation_bank_directory: Path | None = None
    semantic_fusion_weight: float = Field(default=0.2, ge=0.0, le=1.0, allow_inf_nan=False)
    historical_indexing: bool = False
    production_reader: bool = False


class ExperimentMatrix(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    configurations: tuple[Path, ...] = Field(min_length=2, max_length=12)
    output: Path
    maximum_cost_usd: Decimal = Field(gt=0, le=25, allow_inf_nan=False)


def validate_options(options: Experiment, *, live_enabled: bool) -> None:
    options = Experiment.model_validate(options.model_dump())
    if not live_enabled:
        raise ValueError("RUN_LIVE_MODEL_TESTS=1 is required for provider evaluation")
    if not options.paths or len(options.paths) != len(set(options.paths)):
        raise ValueError("Experiment requires distinct recall paths")
    if options.channel != "sparse" and (
        options.local_configuration is None or options.model_directory is None
    ):
        raise ValueError("Semantic evaluation requires an explicit verified local model")
    if options.output.exists():
        raise ValueError("Evaluation output already exists; prior failures must be preserved")
    if options.storage == "postgres" and options.execution != "runtime":
        raise ValueError("PostgreSQL evaluation requires the actual runtime")
    if options.search_planning == "reader" and options.execution != "runtime":
        raise ValueError("Reader-planned search requires the actual runtime")
    if options.formation_bank_directory is not None and options.pipeline != "formation":
        raise ValueError("A formation bank requires the formation pipeline")


async def run_experiment(
    options: Experiment, *, shared_budget: EvaluationBudget | None = None
) -> dict[str, object]:
    from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
    from agent_core.config import PACKAGE_ROOT, load_settings
    from agent_core.domain.local_embeddings import LocalEmbeddingConfig
    from agent_core.evals.general_recall_composition import (
        local_embedding_provider,
        provider_adapters,
    )
    from agent_core.evals.general_recall_corpus import corpus_digest, load_corpus
    from agent_core.evals.general_recall_database import validate_evaluation_database
    from agent_core.evals.general_recall_execution import evaluate_fixture
    from agent_core.evals.people_execution import BudgetedProvider, EvaluationBudget
    from agent_core.memory.reconsolidation_evidence import implementation_digest
    from agent_core.model.registry import ProviderRegistry, StaticModelRouter

    validate_options(options, live_enabled=os.environ.get("RUN_LIVE_MODEL_TESTS") == "1")
    if shared_budget is not None and (
        shared_budget.maximum != options.maximum_cost_usd
        or shared_budget.failed
        or shared_budget.held
    ):
        raise ValueError("The shared experiment budget changed, failed or retains uncertain usage")
    cases = load_corpus(options.corpus)
    if any(case.split != "development" for case in cases):
        raise ValueError("This development runner cannot consume an unreviewed holdout")
    environment = dict(os.environ)
    environment.setdefault("DATABASE_URL", "postgresql+asyncpg://127.0.0.1:1/unused")
    environment.setdefault("DEPLOYMENT_MODE", "development")
    environment.setdefault("SANDBOX_MECHANISM", "fake")
    settings = load_settings(environment)
    if options.storage == "postgres":
        validate_evaluation_database(settings, authorized=options.disposable_database)
    registry = ProviderRegistry.load(
        PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS, overlay_root=settings.config_dir
    )
    resolved = await StaticModelRouter(registry, system_clock()).resolve(
        options.model_policy, tenant_id="evaluation"
    )
    root = Path(__file__).resolve().parents[3]
    implementation = implementation_digest(root)
    options.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (options.output / "corpus.jsonl").write_text(
        "".join(case.model_dump_json() + "\n" for case in cases)
    )
    budget = shared_budget or EvaluationBudget(
        options.maximum_cost_usd, options.output / "provider-costs.jsonl"
    )
    spent_before, calls_before = budget.spent, budget.calls
    metadata: dict[str, object] = {
        "version": "general-recall-development@5",
        "activation_evidence": False,
        "state": "registered",
        "registered_at": system_clock().now().isoformat(),
        "corpus_sha256": corpus_digest(cases),
        "implementation_sha256": implementation,
        "options": options.model_dump(mode="json"),
        "model": resolved.model_dump(mode="json", exclude={"credential_ref"}),
        "setting": "Synthetic scratch PostgreSQL histories with actual worker timing"
        if options.storage == "postgres"
        else (
            "Synthetic isolated histories; in-memory, not production latency or release evidence"
        ),
        "cases": [case.id for case in cases],
        "completed": 0,
        "shared_budget_journal": None if shared_budget is None else str(budget.journal),
    }
    run_file = options.output / "run.json"
    run_file.write_text(json.dumps(metadata, indent=2) + "\n")
    providers = {}
    local = None
    embeddings = None
    try:
        providers = provider_adapters(settings, registry)
        if options.channel != "sparse":
            assert options.local_configuration is not None and options.model_directory is not None
            local = LocalEmbeddingConfig.model_validate_json(
                options.local_configuration.read_bytes()
            )
            embeddings = local_embedding_provider(options.model_directory, local)
        provider = BudgetedProvider(providers[resolved.provider], budget)
        completed = 0
        async with asyncio.timeout(options.deadline_seconds):
            with (options.output / "observations.jsonl").open("x") as stream:
                for case in cases:
                    for path in options.paths:
                        row = await evaluate_fixture(
                            historical_indexing=options.historical_indexing,
                            production_reader=options.production_reader,
                            case=case,
                            settings=settings,
                            pipeline=options.pipeline,
                            execution=options.execution,
                            storage=options.storage,
                            disposable_database=options.disposable_database,
                            channel=options.channel,
                            path=path,
                            reader=provider,
                            resolved=resolved,
                            noise=options.noise,
                            local=local,
                            embeddings=embeddings,
                            recover_sources=options.source_recovery,
                            semantic_fusion_weight=options.semantic_fusion_weight,
                            reranking=options.reranking,
                            query_planning=options.query_planning,
                            search_planning=options.search_planning,
                            formation_policy=options.formation_policy,
                            formation_bank_path=None
                            if options.formation_bank_directory is None
                            else options.formation_bank_directory
                            / (hashlib.sha256(case.id.encode()).hexdigest() + ".json"),
                        )
                        row["observed_at"] = system_clock().now().isoformat()
                        stream.write(json.dumps(row) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                        completed += 1
                        metadata["completed"] = completed
                        print(
                            json.dumps(
                                {
                                    "completed": completed,
                                    "case": case.id,
                                    "path": path,
                                    "answer_success": row["answer_success"],
                                }
                            ),
                            flush=True,
                        )
        if implementation_digest(root) != implementation:
            raise ValueError("Implementation changed during evaluation")
        metadata["state"] = "completed"
        return metadata
    except BaseException as error:
        metadata.update(state="failed", error_class=type(error).__name__)
        raise
    finally:
        metadata.update(
            actual_cost_usd=str(budget.spent - spent_before),
            held_cost_usd=str(budget.held),
            provider_calls=budget.calls - calls_before,
            finished_at=system_clock().now().isoformat(),
        )
        run_file.write_text(json.dumps(metadata, indent=2) + "\n")
        if embeddings is not None:
            await embeddings.close()
        for adapter in providers.values():
            await adapter.close()


async def run_matrix(
    options: tuple[Experiment, ...], *, output: Path, maximum_cost_usd: Decimal
) -> None:
    from agent_core.evals.people_execution import EvaluationBudget
    from agent_core.memory.reconsolidation_evidence import implementation_digest

    if (
        not 2 <= len(options) <= 12
        or output.exists()
        or len({option.output.resolve() for option in options}) != len(options)
    ):
        raise ValueError("A matrix requires distinct arms and a new output directory")
    paired_fields = (
        "corpus",
        "pipeline",
        "storage",
        "noise",
        "model_policy",
        "paths",
        "formation_policy",
        "formation_bank_directory",
        "maximum_cost_usd",
    )
    for option in options:
        validate_options(option, live_enabled=os.environ.get("RUN_LIVE_MODEL_TESTS") == "1")
        if option.maximum_cost_usd != maximum_cost_usd or any(
            getattr(option, field) != getattr(options[0], field) for field in paired_fields
        ):
            raise ValueError("Paired experiment inputs and shared budget must match")
    if options[0].pipeline == "formation" and options[0].formation_bank_directory is None:
        raise ValueError("A paired formation matrix requires a shared formation bank")
    root = Path(__file__).resolve().parents[3]
    implementation = implementation_digest(root)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    budget = EvaluationBudget(maximum_cost_usd, output / "provider-costs.jsonl")
    metadata: dict[str, object] = {
        "version": "general-recall-development-matrix@1",
        "activation_evidence": False,
        "registered_at": system_clock().now().isoformat(),
        "implementation_sha256": implementation,
        "maximum_cost_usd": str(maximum_cost_usd),
        "experiments": [option.model_dump(mode="json") for option in options],
        "state": "registered",
        "completed_arms": 0,
    }
    manifest = output / "matrix.json"
    manifest.write_text(json.dumps(metadata, indent=2) + "\n")
    try:
        for index, option in enumerate(options):
            if implementation_digest(root) != implementation:
                raise ValueError("Implementation changed between matrix arms")
            await run_experiment(option, shared_budget=budget)
            metadata["completed_arms"] = index + 1
        metadata["state"] = "completed"
    except BaseException as error:
        metadata.update(state="failed", error_class=type(error).__name__)
        raise
    finally:
        metadata.update(
            actual_cost_usd=str(budget.spent),
            held_cost_usd=str(budget.held),
            provider_calls=budget.calls,
            finished_at=system_clock().now().isoformat(),
        )
        manifest.write_text(json.dumps(metadata, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--configuration", type=Path)
    source.add_argument("--matrix", type=Path)
    args = parser.parse_args()
    try:
        if args.matrix is not None:
            matrix = ExperimentMatrix.model_validate_json(args.matrix.read_bytes())
            asyncio.run(
                run_matrix(
                    tuple(
                        Experiment.model_validate_json(path.read_bytes())
                        for path in matrix.configurations
                    ),
                    output=matrix.output,
                    maximum_cost_usd=matrix.maximum_cost_usd,
                )
            )
            result = json.loads((matrix.output / "matrix.json").read_bytes())
        else:
            options = Experiment.model_validate_json(args.configuration.read_bytes())
            result = asyncio.run(run_experiment(options))
    except Exception as error:
        raise SystemExit(
            f"Evaluation stopped: {type(error).__name__}; retained output records the attempt"
        ) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
