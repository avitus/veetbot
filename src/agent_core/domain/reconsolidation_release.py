"""Closed, content-free release certificate produced from scored observations."""

from typing import Literal

from pydantic import Field, model_validator

from agent_core.domain.reconsolidation import ReconValue
from agent_core.domain.reconsolidation_merge import Digest


class ReconsolidationQuality(ReconValue):
    split: Literal["development", "holdout"]
    repeats: Literal[3] = 3
    false_merges: Literal[0]
    lost_facts: Literal[0]
    boundary_failures: Literal[0]
    failures: Literal[0]
    duplicate_coverage: float = Field(ge=0.8, le=1)
    hypothesis_precision: float = Field(ge=0.8, le=1)
    hypothesis_recall: float = Field(ge=0.6, le=1)
    answer_coverage_lift: float = Field(ge=0.1, le=1)
    merge_answer_regressions: Literal[0]


class ReconsolidationEvidence(ReconValue):
    schema_version: Literal[1] = 1
    policy: Literal["reconsolidation@1"] = "reconsolidation@1"
    withdrawn: Literal[False] = False
    build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    implementation_sha256: Digest
    corpus_sha256: Digest
    scorer_sha256: Digest
    report_sha256: Digest
    recall_report_sha256: Digest
    recall_corpus_sha256: Digest
    recall_build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    provider: str
    model: str
    reasoning_effort: Literal["provider-default"] = "provider-default"
    model_configuration_sha256: Digest
    privacy_sha256: Digest
    formation_policy: str
    retrieval_policy: str
    upstream_build_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    upstream_corpus_sha256: Digest
    quality: tuple[ReconsolidationQuality, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def complete_comparison(self) -> "ReconsolidationEvidence":
        if self.recall_build_ref != self.build_ref:
            raise ValueError("recall floor evidence must cover the deployed revision")
        if {r.split for r in self.quality} != {"development", "holdout"}:
            raise ValueError("evidence requires both complete splits")
        return self
