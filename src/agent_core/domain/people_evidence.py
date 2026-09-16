"""Content-free, version-bound evidence required for People policy activation."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PeopleQualityMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    identity_precision: float = Field(ge=0.995, le=1)
    resolvable_identity_recall: float = Field(ge=0.9, le=1)
    collision_false_merges: Literal[0]
    direct_fact_recall: float = Field(ge=0.95, le=1)
    direct_fact_precision: float = Field(ge=0.95, le=1)
    direction_accuracy: float = Field(ge=0.95, le=1)
    attribution_accuracy: float = Field(ge=0.95, le=1)
    temporal_accuracy: float = Field(ge=0.95, le=1)
    task_success: float = Field(ge=0.9, le=1)
    retrieval_evidence_recall: float = Field(ge=0.95, le=1)
    ordinary_memory_regressions: Literal[0]
    boundary_failures: Literal[0]
    abstentions: int = Field(ge=0)
    unambiguous_decisions: int = Field(ge=1)
    # The inherited candidate-policy floors are measured on the unchanged
    # ordinary-memory corpus, not borrowed from a previous artifact.
    inherited_direct_recall: float = Field(ge=0.95, le=1)
    inherited_hypothesis_recall: float = Field(ge=0.8, le=1)
    inherited_benign_precision: float = Field(ge=0.9, le=1)
    inherited_disposition_precision: float = Field(ge=0.75, le=1)
    cost_usd: Annotated[str, Field(pattern=r"^\d+(?:\.\d+)?$")]
    provider_calls: int = Field(ge=3)
    segments: int = Field(ge=1)

    @model_validator(mode="after")
    def call_budget(self) -> "PeopleQualityMetrics":
        if self.provider_calls != 3 * self.segments:
            raise ValueError("People formation must make three calls per segment")
        return self


class PeopleFormationEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1]
    formation_policy_version: Literal["formation@11"]
    extractor_version: Literal["people-assisted-v1"]
    linker_version: Literal["people-linker@1"]
    resolver_version: Literal["people-resolver@1"]
    scorer_version: Literal["people-scorer@1"]
    schema_sha256: Digest
    implementation_sha256: Digest
    corpus_sha256: Digest
    holdout_sha256: Digest
    ordinary_corpus_sha256: Digest
    ordinary_holdout_sha256: Digest
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    model_policy: str = Field(min_length=1)
    reasoning_configuration: str = Field(min_length=1)
    policy_profile: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    build_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    development_scenarios: int = Field(ge=120)
    holdout_scenarios: int = Field(ge=60)
    labeled_mentions: int = Field(ge=1000)
    collision_cases: int = Field(ge=100)
    repeats: int = Field(ge=3)
    run_metrics: list[PeopleQualityMetrics] = Field(min_length=3)
    holdout_metrics: list[PeopleQualityMetrics] = Field(min_length=3)
    comparative_pipelines: tuple[
        Literal["current-memory"], Literal["identity-links"], Literal["full-people"]
    ]
    paired_improvement_ci95_low: float = Field(gt=0, le=1)
    paired_improvement_ci95_high: float = Field(gt=0, le=1)
    owner_people_count: int = Field(ge=20, le=30)
    owner_task_count: int = Field(ge=50)
    owner_useful_correct: float = Field(ge=0.9, le=1)
    owner_harmful_mixups: Literal[0]
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def complete_comparison(self) -> "PeopleFormationEvidence":
        if len(self.run_metrics) != self.repeats or len(self.holdout_metrics) != self.repeats:
            raise ValueError("each repeat needs development and holdout measurements")
        if self.paired_improvement_ci95_low > self.paired_improvement_ci95_high:
            raise ValueError("paired improvement interval is reversed")
        if self.corpus_sha256 == self.holdout_sha256:
            raise ValueError("development and holdout must be distinct frozen corpora")
        return self
