"""M33 release observations; no source prose, credentials or claimed pass flag."""

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Family = Literal[
    "attributes",
    "preferences",
    "relationships",
    "goals",
    "decisions",
    "routines",
    "temporal",
    "complex",
]
PathKind = Literal["automatic", "deliberate"]
Pipeline = Literal["seeded", "formation"]
Channel = Literal["sparse", "dense", "hybrid"]
Action = Literal["answer", "clarify", "abstain"]


class ReleaseValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecallReleaseIdentity(ReleaseValue):
    build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    implementation_sha256: Digest
    corpus_sha256: Digest
    scenario_manifest_sha256: Digest
    scorer_sha256: Digest
    # Full effective policy, profiles, context budgets and trusted authority rules.
    configuration_sha256: Digest
    embedding_configuration_sha256: Digest
    formation_configuration_sha256: Digest
    reader_configuration_sha256: Digest
    index_manifest_sha256: Digest
    independence_review_sha256: Digest
    preregistration_sha256: Digest
    frozen_controls_sha256: Digest
    evaluated_inputs_sha256: Digest | None = None
    execution_manifest_sha256: Digest | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_identity(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result: dict[str, Any] = handler(self)
        if self.evaluated_inputs_sha256 is None:
            result.pop("evaluated_inputs_sha256", None)
        if self.execution_manifest_sha256 is None:
            result.pop("execution_manifest_sha256", None)
        return result


class RecallPreregistration(ReleaseValue):
    registered_at: AwareDatetime
    baseline_report_sha256: Digest
    context_tokens: int = Field(gt=0, le=100000)
    p95_latency_ms: float = Field(gt=0, allow_inf_nan=False)
    maximum_case_usd: float = Field(gt=0, allow_inf_nan=False)


class RecallExpectedSource(ReleaseValue):
    source_id: Identifier
    text_sha256: Digest


class RecallSourceWitness(RecallExpectedSource):
    session_id: UUID
    event_sequence: int = Field(gt=0)
    belief_id: UUID
    # Independent per-query ranks do not establish one combined ordering.
    anchor_rank: int | None = Field(gt=0, le=10000)
    rendered: bool


class RecallScenario(ReleaseValue):
    id: Identifier
    family: Family
    split: Literal["development", "holdout"]
    scenario_lineage: Identifier
    query_lineage: Identifier
    history_sha256: Digest
    required_evidence: tuple[Identifier, ...] = Field(max_length=20)
    required_answer_atoms: tuple[Identifier, ...] = Field(max_length=100)
    expected_action: Action
    implicit_task: bool
    known_incident: bool = False
    evidence_sources: dict[Identifier, tuple[RecallExpectedSource, ...]] = Field(
        default_factory=dict, max_length=100
    )


class RecallObservation(ReleaseValue):
    identity_sha256: Digest
    scenario_id: Identifier
    path: PathKind
    pipeline: Pipeline
    channel: Channel
    observed_at: AwareDatetime
    # IDs and mechanically scored answer atoms, produced after retrieval/reading.
    formed: tuple[Identifier, ...] = Field(max_length=10000)
    candidates: tuple[Identifier, ...] = Field(max_length=10000)
    ranked: tuple[Identifier, ...] = Field(max_length=20)
    rendered: tuple[Identifier, ...] = Field(max_length=20)
    recovered_sources: tuple[RecallSourceWitness, ...] = Field(default=(), max_length=12)
    rendered_noise_items: int | None = Field(default=None, ge=0, le=20)
    global_rank_certifiable: bool | None = None
    answer_atoms: tuple[Identifier, ...] = Field(max_length=100)
    action: Action | None
    unsupported_atoms: tuple[Identifier, ...] = Field(max_length=100)
    invalid_current_ids: tuple[Identifier, ...] = Field(max_length=100)
    boundary_failures: tuple[Identifier, ...] = Field(max_length=100)
    searches: int = Field(ge=0, le=4)
    rounds: int = Field(ge=0, le=2)
    context_tokens: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)
    cost_usd: float = Field(ge=0, allow_inf_nan=False)


class RecallGateReceipt(ReleaseValue):
    identity_sha256: Digest
    # Gate 12 is admission itself; the other eleven require reviewed receipts.
    gate: int = Field(ge=1, le=11)
    report_sha256: Digest
    passed: int = Field(gt=0)
    failed: int = Field(ge=0)


class RecallReleaseBundle(ReleaseValue):
    version: Literal["general-recall-release@1", "general-recall-release@2"]
    identity: RecallReleaseIdentity
    preregistration: RecallPreregistration
    scenarios: tuple[RecallScenario, ...] = Field(min_length=400, max_length=10000)
    observations: tuple[RecallObservation, ...] = Field(min_length=4800, max_length=120000)
    gates: tuple[RecallGateReceipt, ...] = Field(min_length=11, max_length=11)
