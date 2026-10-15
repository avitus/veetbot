"""Preregister and retain the complete M33 matrix under one approved allowance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from agent_core.config import PACKAGE_ROOT, DeploymentMode, Settings
from agent_core.domain.general_recall_release import RecallPreregistration, RecallReleaseIdentity
from agent_core.domain.local_embeddings import LocalEmbeddingConfig
from agent_core.domain.messages import ResolvedModel
from agent_core.evals.general_recall_confirmation import (
    ConfirmationPrincipal,
    ConfirmedExecution,
    capture_runtime_configurations,
)
from agent_core.evals.general_recall_corpus import RecallFixture
from agent_core.evals.general_recall_experiment import Experiment
from agent_core.memory.semantic_deployment import IndexConfiguration, configuration_digest


class ConfirmationOptions(Experiment):
    production_reader: Literal[True] = True
    # This task's separately approved aggregate cap, never twelve arm allowances.
    maximum_cost_usd: Decimal = Field(default=Decimal("100"), gt=0, le=300, allow_inf_nan=False)
    # Whole-matrix setup/indexing time, separate from the answer-latency gate.
    deadline_seconds: int = Field(default=86400, ge=1, le=604800)
    independence_review: Path
    baseline_report: Path
    build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    context_tokens: int = Field(default=2000, gt=0, le=100000)
    p95_latency_ms: float = Field(gt=0, allow_inf_nan=False)
    maximum_case_usd: float = Field(gt=0, allow_inf_nan=False)
    parallel_scratch_database: str | None = Field(
        default=None, pattern=r"^veetbot_m33_eval_[a-z0-9_]{1,32}$"
    )


@dataclass(frozen=True)
class PreparedConfirmation:
    confirmation: ConfirmedExecution
    preregistration: RecallPreregistration
    cases: tuple[RecallFixture, ...]
    local: LocalEmbeddingConfig
    model_directory: Path
    resolved: ResolvedModel
    settings: Settings
    snapshots: dict[str, Any]
    original_artifacts: dict[str, bytes] = field(default_factory=dict)
    worker_settings: tuple[Settings, ...] = ()


async def prepare_confirmation(
    root: Path, options: ConfirmationOptions, settings: Settings, scratch: Settings
) -> PreparedConfirmation:
    from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
    from agent_core.domain.model_settings import ModelSettingsCatalog
    from agent_core.evals.general_recall_capture import capture_deployment, capture_schema
    from agent_core.evals.general_recall_collection import scenario_manifest
    from agent_core.evals.general_recall_composition import system_clock
    from agent_core.evals.general_recall_corpus import corpus_digest, load_corpus
    from agent_core.evals.general_recall_database import confirmation_databases
    from agent_core.evals.general_recall_publication import IndependenceReview
    from agent_core.evals.general_recall_runtime import reader_runtime_inputs
    from agent_core.evals.memory_distillation import require_committed_tree
    from agent_core.memory.general_recall_evidence import _read_bounded, scorer_digest
    from agent_core.memory.recall_release_binding import capture_inputs
    from agent_core.memory.reconsolidation_evidence import implementation_digest
    from agent_core.model.registry import ProviderRegistry, StaticModelRouter

    try:
        review_bytes = _read_bounded(options.independence_review)
        review = IndependenceReview.model_validate_json(review_bytes)
    except (OSError, ValueError) as error:
        raise ValueError("Confirmation requires an independent accepted corpus review") from error
    if review.reviewed_at > system_clock().now():
        raise ValueError("Corpus review cannot be dated in the future")
    require_committed_tree(root, options.build_ref)
    if (
        options.execution != "runtime"
        or options.storage != "postgres"
        or set(options.paths) != {"automatic", "deliberate"}
        or options.formation_bank_directory is not None
    ):
        raise ValueError("Confirmation requires both PostgreSQL runtime paths and fresh banks")
    # The review, source tree, intended deployment graph, actual migrated schema,
    # verified encoder and frozen controls all precede held-out task parsing.
    workers = confirmation_databases(
        scratch,
        additional=options.parallel_scratch_database,
        authorized=options.disposable_database,
    )
    baseline = _read_bounded(options.baseline_report)
    deployment = await capture_deployment(
        settings, scratch, authorized=options.disposable_database, model_policy=options.model_policy
    )
    profile = IndexConfiguration.model_validate(deployment["indexing"])
    assert profile.local is not None and profile.model_directory is not None
    authority = ConfirmationPrincipal.model_validate(
        deployment["composition"]["configuration"]["principal"]
    )
    schema = await capture_schema(scratch.database_url)
    if deployment["composition"]["configuration"]["schema_sha256"] != configuration_digest(schema):
        raise ValueError("Deployment schema changed during capture")
    controls = capture_frozen_controls(root)
    registry = ProviderRegistry.load(
        PACKAGE_ROOT / "models", adapters=ADAPTER_DEFINITIONS, overlay_root=settings.config_dir
    )
    router = StaticModelRouter(registry, system_clock())
    resolved = await router.resolve(options.model_policy, tenant_id=authority.tenant)
    reader_runtime = reader_runtime_inputs(
        resolved,
        catalog=ModelSettingsCatalog.model_validate(deployment["composition"]["reader"]["catalog"]),
        router=router,
    )
    if profile.reader_sha256 != reader_runtime["reader_sha256"]:
        raise ValueError("The measured reader differs from the intended admitted reader")
    effective = replace(
        settings,
        database_url=scratch.database_url,
        deployment_mode=DeploymentMode.DEVELOPMENT,
        artifact_root=scratch.artifact_root,
    )
    manifest = await capture_runtime_configurations(
        options,
        settings=effective,
        principal=authority.principal(),
        resolved=resolved,
        local=profile.local,
        maximum_cost_usd=options.maximum_cost_usd,
    )
    worker_settings = tuple(
        replace(effective, database_url=value.database_url) for value in workers
    )
    for worker in worker_settings[1:]:
        if await capture_schema(worker.database_url) != schema:
            raise ValueError("Confirmation worker schema differs from the captured schema")
        other = await capture_runtime_configurations(
            options,
            settings=worker,
            principal=authority.principal(),
            resolved=resolved,
            local=profile.local,
            maximum_cost_usd=options.maximum_cost_usd,
        )
        if other.model_dump(exclude={"captured_at"}) != manifest.model_dump(
            exclude={"captured_at"}
        ):
            raise ValueError("Confirmation worker runtime configuration differs")
    if len(worker_settings) == 2:
        manifest = type(manifest).model_validate(
            {**manifest.model_dump(), "version": "recall-execution-manifest@2", "worker_count": 2}
        )
    target = deployment["composition"]
    if (
        target["configuration"]["context"]["classes"]["in_turn_recall"]["max_tokens"]
        != options.context_tokens
    ):
        raise ValueError("Preregistered context ceiling differs from actual recall configuration")
    for variant in manifest.variants:
        inputs = variant.configuration
        if not inputs["production_reader"] or inputs["reader_runtime"] != reader_runtime:
            raise ValueError("Measured reader settings differ from the intended deployment")
        if inputs["composition"]["configuration"]["schema_sha256"] != configuration_digest(schema):
            raise ValueError("Measured schema differs from the deployment schema")
        if variant.channel == "hybrid" and (
            inputs["retrieval"] != target["configuration"]["memory"]["retrieval"]
            or inputs["embedding"] != profile.embedding.model_dump(mode="json")
            or inputs["source_recovery"] != inputs["retrieval"]["source_recovery_enabled"]
        ):
            raise ValueError("Measured recall settings differ from the intended deployment")
        if (
            variant.pipeline == "formation"
            and inputs["composition"]["formation"] != target["formation"]
        ):
            raise ValueError("Measured formation differs from the intended deployment")
    preregistration = RecallPreregistration(
        registered_at=system_clock().now(),
        baseline_report_sha256=_digest(baseline),
        context_tokens=options.context_tokens,
        p95_latency_ms=options.p95_latency_ms,
        maximum_case_usd=options.maximum_case_usd,
    )
    identity = RecallReleaseIdentity(
        build_ref=options.build_ref,
        implementation_sha256=implementation_digest(root),
        corpus_sha256=review.corpus_sha256,
        scenario_manifest_sha256=review.scenario_manifest_sha256,
        scorer_sha256=scorer_digest(root),
        **deployment["dependencies"],
        index_manifest_sha256=configuration_digest(schema),
        independence_review_sha256=_digest(review_bytes),
        preregistration_sha256=_digest(preregistration.model_dump_json().encode()),
        frozen_controls_sha256=configuration_digest(controls),
        execution_manifest_sha256=manifest.digest,
    )
    evaluated = capture_inputs(root, identity.model_dump(mode="json"))
    identity = identity.model_copy(update={"evaluated_inputs_sha256": evaluated.digest})
    cases = load_corpus(options.corpus, release=True)
    scenarios = scenario_manifest(cases)
    if (
        corpus_digest(cases) != review.corpus_sha256
        or _digest(b"".join(s.model_dump_json().encode() + b"\n" for s in scenarios))
        != review.scenario_manifest_sha256
        or len(set(review.reviewed_scenarios)) != len(review.reviewed_scenarios)
        or set(review.reviewed_scenarios) != {case.id for case in cases}
    ):
        raise ValueError("Corpus differs from the independently reviewed scenarios")
    if capture_inputs(root, identity.model_dump(mode="json")) != evaluated:
        raise ValueError("Evaluated inputs changed during preflight")
    return PreparedConfirmation(
        ConfirmedExecution(identity, manifest),
        preregistration,
        cases,
        profile.local,
        profile.model_directory,
        resolved,
        effective,
        {
            "deployment": deployment,
            "schema": schema,
            "frozen_controls": controls,
            "evaluated_inputs": evaluated.model_dump(mode="json"),
            "review": json.loads(review_bytes),
            "baseline_sha256": _digest(baseline),
        },
        {"independence-review.json": review_bytes, "baseline-report.json": baseline},
        worker_settings,
    )


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def capture_frozen_controls(root: Path) -> dict[str, str]:
    """Fixed M16/M32 inventory; callers cannot omit an inconvenient control."""
    from agent_core.memory.general_recall_evidence import _read_bounded

    required = {
        "memory-benchmark.v1.json",
        "memory-benchmark.baseline.json",
        "memory-reconsolidation.v1.json",
        "memory-reconsolidation.manifest.json",
        "memory-reconsolidation-control.v1.json",
        "memory-reconsolidation-control.manifest.json",
    }
    directory = root / "evals/capability"
    paths = {
        path
        for pattern in ("memory-benchmark*", "memory-reconsolidation*")
        for path in directory.glob(pattern)
        if path.is_file()
    }
    if not required <= {path.name for path in paths}:
        raise ValueError("Frozen M16/M32 controls are missing")
    paths.update(
        path
        for path in (root / "evals/observations/memory-reconsolidation").rglob("*")
        if path.is_file()
    )
    return {str(path.relative_to(root)): _digest(_read_bounded(path)) for path in sorted(paths)}


async def verify_external_inputs(
    root: Path,
    options: ConfirmationOptions,
    settings: Settings,
    scratch: Settings,
    prepared: PreparedConfirmation,
) -> None:
    from agent_core.evals.general_recall_capture import capture_deployment, capture_schema

    deployment = await capture_deployment(
        settings, scratch, authorized=options.disposable_database, model_policy=options.model_policy
    )
    schema = await capture_schema(scratch.database_url)
    if (
        deployment != prepared.snapshots["deployment"]
        or schema != prepared.snapshots["schema"]
        or capture_frozen_controls(root) != prepared.snapshots["frozen_controls"]
    ):
        raise ValueError("Captured deployment, schema or frozen controls changed")
    for worker in prepared.worker_settings[1:]:
        if (
            await capture_schema(worker.database_url) != schema
            or await capture_deployment(
                settings,
                worker,
                authorized=options.disposable_database,
                model_policy=options.model_policy,
            )
            != deployment
        ):
            raise ValueError("Captured parallel worker inputs changed")


async def run_confirmation(
    root: Path,
    options: ConfirmationOptions,
    settings: Settings,
    scratch: Settings,
    *,
    live_enabled: bool,
) -> dict[str, Any]:
    from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
    from agent_core.evals.general_recall_collection import CollectionArtifact, CollectionManifest
    from agent_core.evals.general_recall_composition import (
        local_embedding_provider,
        provider_adapters,
        system_clock,
    )
    from agent_core.evals.general_recall_database import confirmation_databases
    from agent_core.evals.general_recall_execution import evaluate_fixture
    from agent_core.evals.people_execution import BudgetedProvider, EvaluationBudget
    from agent_core.memory.recall_release_binding import capture_inputs, input_stamps
    from agent_core.model.registry import ProviderRegistry

    options = ConfirmationOptions.model_validate(options.model_dump())
    if not live_enabled:
        raise ValueError("RUN_LIVE_MODEL_TESTS=1 is required for confirmation")
    if options.output.exists():
        raise ValueError("Confirmation output already exists; preserve every prior attempt")
    prepared = await prepare_confirmation(root, options, settings, scratch)
    confirmation = prepared.confirmation
    workers = prepared.worker_settings or (prepared.settings,)
    expected_workers = confirmation_databases(
        prepared.settings,
        additional=options.parallel_scratch_database,
        authorized=options.disposable_database,
    )
    if workers != expected_workers or len(workers) != (confirmation.manifest.worker_count or 1):
        raise ValueError("Confirmation worker settings differ from the registered manifest")
    identity = confirmation.identity
    initial_stamps = input_stamps(root)
    output = options.output
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    (output / "observations").mkdir()
    (output / "banks").mkdir()
    _write(output / "identity.json", identity.model_dump(mode="json"))
    _write(output / "preregistration.json", prepared.preregistration.model_dump(mode="json"))
    _write(output / "inputs.json", prepared.snapshots)
    _write(output / "confirmation.json", confirmation.manifest.model_dump(mode="json"))
    for name, content in prepared.original_artifacts.items():
        with (output / name).open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    (output / "corpus.jsonl").write_text(
        "".join(case.model_dump_json() + "\n" for case in prepared.cases)
    )
    budget = EvaluationBudget(options.maximum_cost_usd, output / "provider-costs.jsonl")
    metadata: dict[str, Any] = {
        "version": "general-recall-confirmation@1",
        "state": "registered",
        "activation_evidence": False,
        "identity_sha256": _digest(identity.model_dump_json().encode()),
        "maximum_cost_usd": str(options.maximum_cost_usd),
        "completed": 0,
        "expected_observations": len(prepared.cases) * 12,
        "options": options.model_dump(mode="json"),
    }
    _write(output / "run.json", metadata)
    providers = {}
    encoders = []
    try:
        registry = ProviderRegistry.load(
            PACKAGE_ROOT / "models",
            adapters=ADAPTER_DEFINITIONS,
            overlay_root=prepared.settings.config_dir,
        )
        await verify_external_inputs(root, options, settings, scratch, prepared)
        if (
            capture_inputs(root, identity.model_dump(mode="json")).digest
            != identity.evaluated_inputs_sha256
        ):
            raise ValueError("Inputs changed before provider construction")
        providers = provider_adapters(prepared.settings, registry)
        for _ in workers:
            encoders.append(local_embedding_provider(prepared.model_directory, prepared.local))
        reader = BudgetedProvider(providers[prepared.resolved.provider], budget)

        async def worker(index: int) -> None:
            try:
                for case in prepared.cases[index :: len(workers)]:
                    if budget.failed:
                        return
                    await measure(case, index)
            except BaseException:
                # Do not cancel another already admitted provider request just
                # because its peer failed. Stop new sends and let it settle.
                async with budget.lock:
                    budget.failed = True
                raise

        async def measure(case: RecallFixture, index: int) -> None:
            key = _digest(case.id.encode())
            with (output / "observations" / f"{key}.jsonl").open("x") as stream:
                for variant in confirmation.manifest.variants:
                    if budget.failed:
                        return
                    if input_stamps(root) != initial_stamps:
                        raise ValueError("Evaluated files changed during confirmation")
                    row = await evaluate_fixture(
                        case=case,
                        settings=workers[index],
                        principal=confirmation.manifest.principal.principal(),
                        pipeline=variant.pipeline,
                        path=variant.path,
                        channel=variant.channel,
                        reader=reader,
                        resolved=prepared.resolved,
                        local=prepared.local,
                        embeddings=encoders[index],
                        noise=options.noise,
                        storage="postgres",
                        disposable_database=options.disposable_database,
                        recover_sources=options.source_recovery,
                        semantic_fusion_weight=options.semantic_fusion_weight,
                        formation_policy=options.formation_policy,
                        reranking=options.reranking,
                        query_planning=options.query_planning,
                        search_planning=options.search_planning,
                        historical_indexing=options.historical_indexing,
                        production_reader=options.production_reader,
                        confirmation=confirmation,
                        formation_bank_path=output / "banks" / f"{key}.json"
                        if variant.pipeline == "formation"
                        else None,
                    )
                    row["observed_at"] = system_clock().now().isoformat()
                    stream.write(json.dumps(row) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    metadata["completed"] += 1
            print(
                json.dumps({"completed": metadata["completed"], "scenario": case.id}),
                flush=True,
            )

        async with asyncio.timeout(options.deadline_seconds):
            outcomes = await asyncio.gather(
                *(worker(index) for index in range(len(workers))), return_exceptions=True
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
        if budget.failed or metadata["completed"] != metadata["expected_observations"]:
            raise ValueError("Confirmation stopped before the complete registered matrix")
        if (
            capture_inputs(root, identity.model_dump(mode="json")).digest
            != identity.evaluated_inputs_sha256
        ):
            raise ValueError("Evaluated files changed during confirmation")
        await verify_external_inputs(root, options, settings, scratch, prepared)
        artifacts = [
            CollectionArtifact.model_validate(
                {
                    "role": role,
                    "path": str(path.relative_to(output)),
                    "sha256": _digest(path.read_bytes()),
                }
            )
            for role, paths in (
                ("corpus", [output / "corpus.jsonl"]),
                ("journal", [budget.journal]),
                ("confirmation", [output / "confirmation.json"]),
                ("observations", sorted((output / "observations").glob("*.jsonl"))),
                ("bank", sorted((output / "banks").glob("*.json"))),
            )
            for path in paths
        ]
        collection = CollectionManifest(
            version="recall-collection@2",
            identity_sha256=metadata["identity_sha256"],
            maximum_cost_usd=options.maximum_cost_usd,
            artifacts=tuple(artifacts),
        )
        _write(output / "collection.json", collection.model_dump(mode="json"))
        metadata["state"] = "completed"
        return metadata
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
        (output / "run.json").write_text(json.dumps(metadata, indent=2) + "\n")
        try:
            for encoder in encoders:
                await encoder.close()
        finally:
            for provider in providers.values():
                await provider.close()


def _write(path: Path, value: dict[str, Any]) -> None:
    with path.open("x") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    import argparse

    from agent_core.config import load_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    args = parser.parse_args()
    try:
        selected = ConfirmationOptions.model_validate_json(args.configuration.read_bytes())
        settings = load_settings(dict(os.environ))
        scratch_url = os.environ.get("M33_SCRATCH_DATABASE_URL")
        if not scratch_url:
            raise ValueError("M33_SCRATCH_DATABASE_URL is required")
        scratch = replace(
            settings, database_url=scratch_url, deployment_mode=DeploymentMode.DEVELOPMENT
        )
        result = asyncio.run(
            run_confirmation(
                Path(__file__).resolve().parents[3],
                selected,
                settings,
                scratch,
                live_enabled=os.environ.get("RUN_LIVE_MODEL_TESTS") == "1",
            )
        )
    except Exception as error:
        # Do not print Settings, credentials, provider errors or connection URLs.
        raise SystemExit(
            f"Confirmation stopped: {type(error).__name__}; existing results are retained"
        ) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
