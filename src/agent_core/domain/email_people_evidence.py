"""Content-free activation evidence for the separately versioned email People bridge."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.people_evidence import Digest, PeopleFormationEvidence


class EmailPeopleMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scenarios: int = Field(ge=60)
    supported_claim_precision: float = Field(ge=0.95, le=1)
    supported_claim_recall: float = Field(ge=0.95, le=1)
    identity_precision: float = Field(ge=0.995, le=1)
    resolvable_identity_recall: float = Field(ge=0.9, le=1)
    attribution_accuracy: float = Field(ge=0.95, le=1)
    direction_accuracy: float = Field(ge=0.95, le=1)
    temporal_accuracy: float = Field(ge=0.95, le=1)
    false_merges: Literal[0]
    draft_completion_failures: Literal[0]
    authority_failures: Literal[0]
    ordinary_email_regressions: Literal[0]
    automatic_older_mail_capture: Literal[0]
    extra_provider_calls: Literal[0]
    assessment_calls: int = Field(ge=1)
    cost_usd: str = Field(pattern=r"^\d+(?:\.\d+)?$")


class OrdinaryEmailEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scorer_version: Literal["email-quality@1"]
    comparison_version: Literal["email-people-ordinary@1"]
    baseline_corpus_sha256: Digest
    candidate_corpus_sha256: Digest
    baseline_implementation_sha256: Digest
    candidate_implementation_sha256: Digest
    threads: int = Field(ge=200)
    snapshots: int = Field(ge=30)
    regressions: Literal[0]
    passed: Literal[True]


class EmailPeopleEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    policy_version: Literal["email-semantic@2"]
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    build_ref: str = Field(pattern=r"^[0-9a-f]{40}$")
    schema_sha256: Digest
    implementation_sha256: Digest
    corpus_sha256: Digest
    holdout_sha256: Digest
    run_sha256: Digest
    ordinary_email: OrdinaryEmailEvidence
    people: PeopleFormationEvidence
    run_metrics: list[EmailPeopleMetrics] = Field(min_length=3)
    holdout_metrics: list[EmailPeopleMetrics] = Field(min_length=3)
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def complete_tuple(self) -> "EmailPeopleEvidence":
        if (self.people.provider, self.people.model, self.people.build_ref) != (
            self.provider,
            self.model,
            self.build_ref,
        ):
            raise ValueError("email and People evidence must identify the same release and model")
        if (
            len(self.run_metrics) != len(self.holdout_metrics)
            or len(self.run_metrics) != self.people.repeats
        ):
            raise ValueError("email People evidence repeat counts must match")
        if self.corpus_sha256 == self.holdout_sha256:
            raise ValueError("email People development and holdout corpora must differ")
        if self.ordinary_email.candidate_implementation_sha256 != self.implementation_sha256:
            raise ValueError("ordinary Email candidate implementation must match People evidence")
        if (
            self.ordinary_email.baseline_corpus_sha256
            == self.ordinary_email.candidate_corpus_sha256
        ):
            raise ValueError("ordinary Email baseline and candidate corpora must differ")
        return self
