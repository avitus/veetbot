"""Bind runtime observations to inputs captured before confirmatory execution.

These records do not attest to human review or grant release admission. The
trusted runner must capture actual configurations before loading held-out tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from agent_core.domain.agents import Principal
from agent_core.domain.general_recall_release import (
    Channel,
    PathKind,
    Pipeline,
    RecallReleaseIdentity,
    ReleaseValue,
)
from agent_core.memory.semantic_deployment import configuration_digest

if TYPE_CHECKING:
    from agent_core.config import Settings
    from agent_core.domain.local_embeddings import LocalEmbeddingConfig
    from agent_core.domain.messages import ResolvedModel
    from agent_core.evals.general_recall_experiment import Experiment


class ConfirmationPrincipal(ReleaseValue):
    tenant: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    scopes: tuple[str, ...]
    roles: tuple[str, ...]

    @model_validator(mode="after")
    def canonical_authority(self) -> Self:
        if self.scopes != tuple(sorted(set(self.scopes))) or self.roles != tuple(
            sorted(set(self.roles))
        ):
            raise ValueError("Confirmation authority must be sorted and unique")
        return self

    def principal(self) -> Principal:
        return Principal(
            tenant_id=self.tenant,
            principal_id=self.owner,
            scopes=set(self.scopes),
            roles=set(self.roles),
        )


class ConfirmationVariant(ReleaseValue):
    pipeline: Pipeline
    path: PathKind
    channel: Channel
    configuration: dict[str, Any]

    @property
    def key(self) -> tuple[str, str, str]:
        return self.pipeline, self.path, self.channel

    @property
    def digest(self) -> str:
        return configuration_digest(self.configuration)


class RecallExecutionManifest(ReleaseValue):
    version: Literal["recall-execution-manifest@1"]
    captured_at: AwareDatetime
    principal: ConfirmationPrincipal
    maximum_cost_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    variants: tuple[ConfirmationVariant, ...] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def complete_runtime_matrix(self) -> Self:
        required = {
            (pipeline, path, channel)
            for pipeline in ("seeded", "formation")
            for path in ("automatic", "deliberate")
            for channel in ("sparse", "dense", "hybrid")
        }
        if {variant.key for variant in self.variants} != required:
            raise ValueError("Confirmation requires every distinct runtime variant")
        authority = self.principal.model_dump(mode="json")
        for variant in self.variants:
            value = variant.configuration
            if value.get("production_reader") is not True:
                raise ValueError("Confirmation requires the production reader configuration")
            captured = value.get("composition")
            composition = captured.get("configuration") if isinstance(captured, dict) else None
            if (
                value.get("version") != "recall-fixture-inputs@1"
                or value.get("execution") != "runtime"
                or value.get("storage") != "postgres"
                or tuple(value.get(k) for k in ("pipeline", "path", "channel")) != variant.key
                or not isinstance(composition, dict)
                or composition.get("principal") != authority
            ):
                raise ValueError("Confirmation requires matching runtime inputs and authority")
            # Hashing also rejects non-finite or non-JSON configuration values.
            _ = variant.digest
        return self

    @property
    def digest(self) -> str:
        return configuration_digest(self.model_dump(mode="json"))


@dataclass(frozen=True)
class ConfirmedExecution:
    identity: RecallReleaseIdentity
    manifest: RecallExecutionManifest

    def validate(self, configuration: dict[str, Any]) -> None:
        # Revalidate nested values: frozen models do not freeze dict contents.
        manifest = RecallExecutionManifest.model_validate(self.manifest.model_dump())
        if (
            self.identity.evaluated_inputs_sha256 is None
            or self.identity.execution_manifest_sha256 != manifest.digest
        ):
            raise ValueError("Recall confirmation identity or captured inputs changed")
        key = tuple(configuration.get(k) for k in ("pipeline", "path", "channel"))
        variant = next((value for value in manifest.variants if value.key == key), None)
        if variant is None or variant.digest != configuration_digest(configuration):
            raise ValueError("Recall confirmation runtime configuration changed")

    def observation_fields(self) -> dict[str, str]:
        return {
            "identity_sha256": sha256(self.identity.model_dump_json().encode()).hexdigest(),
            "execution_manifest_sha256": self.manifest.digest,
        }

    def validate_observation(self, raw: dict[str, Any]) -> None:
        key = tuple(raw.get(k) for k in ("pipeline", "path", "channel"))
        variant = next((value for value in self.manifest.variants if value.key == key), None)
        if variant is None:
            raise ValueError("Recall confirmation observation variant changed")
        self.validate(variant.configuration)
        if (
            any(raw.get(key) != value for key, value in self.observation_fields().items())
            or raw.get("configuration_sha256") != variant.digest
        ):
            raise ValueError("Recall confirmation observation was unbound or changed")


async def capture_runtime_configurations(
    options: Experiment,
    *,
    settings: Settings,
    principal: Principal,
    resolved: ResolvedModel,
    local: LocalEmbeddingConfig,
    maximum_cost_usd: Decimal,
) -> RecallExecutionManifest:
    """Describe every actual scratch runtime using one fixed non-corpus probe.

    This opens no corpus or encoder asset and constructs no external provider.
    Bootstrap may write the explicitly authorized scratch database; this is not
    a read-only production inspection. The outer runner still owns independent
    deployment, corpus-review, asset, schema and control checks.
    """
    import asyncio
    import json
    import tempfile
    from collections.abc import AsyncIterator
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse
    from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest
    from agent_core.evals.general_recall_composition import system_clock
    from agent_core.evals.general_recall_corpus import HistoryTurn, RecallFixture, TaskInput
    from agent_core.evals.general_recall_execution import evaluate_fixture
    from agent_core.evals.people_execution import BudgetedProvider, EvaluationBudget

    if options.storage != "postgres" or options.execution != "runtime":
        raise ValueError("Confirmation capture requires the PostgreSQL runtime")
    if not options.production_reader:
        raise ValueError("Confirmation capture requires the production reader configuration")

    class CaptureStoppedError(Exception):
        pass

    class NoProviderWork:
        name = resolved.provider

        def stream(
            self, request: ModelRequest, model: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            raise ValueError("Confirmation capture cannot execute model work")

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            raise ValueError("Confirmation capture cannot execute encoder work")

        async def close(self) -> None:
            return

    at = datetime(2026, 1, 1, tzinfo=UTC)
    probe = RecallFixture(
        id="configuration-preflight",
        family="attributes",
        split="development",
        scenario_lineage="configuration-preflight",
        query_lineage="configuration-preflight",
        implicit_task=False,
        history=(HistoryTurn(id="probe", session="probe", at=at, content="Configuration probe"),),
        task=TaskInput(
            question="Configuration probe",
            at=at + timedelta(days=1),
            output_fields={"probe": "Configuration probe"},
        ),
        expected_action="abstain",
        expected_values={"probe": ()},
    )
    variants: list[ConfirmationVariant] = []

    def capture(configuration: dict[str, Any]) -> None:
        variants.append(
            ConfirmationVariant.model_validate(
                {
                    **{key: configuration[key] for key in ("pipeline", "path", "channel")},
                    "configuration": json.loads(json.dumps(configuration)),
                }
            )
        )
        raise CaptureStoppedError

    pipelines: tuple[Pipeline, ...] = ("seeded", "formation")
    paths: tuple[PathKind, ...] = ("automatic", "deliberate")
    channels: tuple[Channel, ...] = ("sparse", "dense", "hybrid")
    inert = NoProviderWork()
    with tempfile.TemporaryDirectory(prefix="m33-capture-") as temporary:
        budget = EvaluationBudget(maximum_cost_usd, Path(temporary) / "unused-journal.jsonl")
        async with asyncio.timeout(options.deadline_seconds):
            for pipeline in pipelines:
                for path in paths:
                    for channel in channels:
                        try:
                            await evaluate_fixture(
                                case=probe,
                                settings=settings,
                                principal=principal,
                                pipeline=pipeline,
                                path=path,
                                channel=channel,
                                reader=BudgetedProvider(inert, budget),
                                resolved=resolved,
                                noise=options.noise,
                                local=local,
                                embeddings=inert,
                                storage="postgres",
                                disposable_database=options.disposable_database,
                                formation_policy=options.formation_policy,
                                recover_sources=options.source_recovery,
                                semantic_fusion_weight=options.semantic_fusion_weight,
                                reranking=options.reranking,
                                query_planning=options.query_planning,
                                search_planning=options.search_planning,
                                historical_indexing=options.historical_indexing,
                                production_reader=options.production_reader,
                                validate_composition=capture,
                            )
                        except CaptureStoppedError:
                            continue
                        raise ValueError("Confirmation capture did not stop before execution")
        if budget.calls or budget.held or budget.spent:
            raise ValueError("Confirmation capture unexpectedly attempted provider work")
    return RecallExecutionManifest(
        version="recall-execution-manifest@1",
        captured_at=system_clock().now(),
        principal=ConfirmationPrincipal(
            tenant=principal.tenant_id,
            owner=principal.principal_id,
            scopes=tuple(sorted(principal.scopes)),
            roles=tuple(sorted(principal.roles)),
        ),
        maximum_cost_usd=maximum_cost_usd,
        variants=tuple(variants),
    )
