"""Frozen models behind the `memory/profiles.yaml` configuration document.

The shipped document is the defaults: every field below repeats the value the
document ships, so a composition with no operator overlay behaves exactly as it
did before the document was loaded, and a static test pins the two together.
The session idle boundary is deliberately absent — it is part of the formation
policy a belief records, and a per-tenant threshold would make two beliefs
formed under the same recorded policy incomparable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_core.config import ConfigurationError
from agent_core.domain.memory import BeliefType

MEMORY_PROFILE_DOCUMENT = "memory/profiles.yaml"


class _ProfileModel(BaseModel):
    """Reject unknown knobs and refuse mutation after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class LifecycleWeights(_ProfileModel):
    """Score multipliers for a belief's lifecycle status."""

    active: float = Field(default=1.0, ge=0.0, le=1.0)
    provisional: float = Field(default=0.4, ge=0.0, le=1.0)


class DecayTauDays(_ProfileModel):
    """Half-life-shaped time constants, in days, per belief type."""

    fact: int = Field(default=30, ge=1)
    preference: int = Field(default=180, ge=1)
    relationship: int = Field(default=365, ge=1)
    user_model_attr: int = Field(default=180, ge=1)
    procedure_pointer: int = Field(default=90, ge=1)

    def for_belief_type(self, belief_type: BeliefType) -> int:
        """The time constant this belief type decays on.

        Ranking and the decay sweep read the same table through one lookup, so
        the age at which a belief stops counting as reinforced is the age at
        which the sweep starts taking its confidence away.
        """

        return {
            BeliefType.FACT: self.fact,
            BeliefType.PREFERENCE: self.preference,
            BeliefType.RELATIONSHIP: self.relationship,
            BeliefType.USER_MODEL_ATTR: self.user_model_attr,
            BeliefType.PROCEDURE_POINTER: self.procedure_pointer,
        }[belief_type]


class UsageDeltas(_ProfileModel):
    """Utility adjustments applied by the usage-feedback sweep."""

    cited_utility_delta: float = Field(default=0.1, ge=0.0, le=1.0)
    uncited_utility_delta: float = Field(default=-0.05, ge=-1.0, le=0.0)


class RetrievalProfile(_ProfileModel):
    """Ranking, fusion, and snapshot-composition knobs for the read path."""

    semantic_enabled: bool = False
    reciprocal_rank_fusion_k: int = Field(default=60, ge=1)
    durable_item_share: float = Field(default=0.6666666667, ge=0.0, le=1.0)
    lifecycle_weights: LifecycleWeights = Field(default_factory=LifecycleWeights)
    decay_tau_days: DecayTauDays = Field(default_factory=DecayTauDays)
    stale_penalty: float = Field(default=0.1, ge=0.0, le=1.0)
    near_duplicate_penalty: float = Field(default=0.1, ge=0.0, le=1.0)
    usage: UsageDeltas = Field(default_factory=UsageDeltas)


class DecayProfile(_ProfileModel):
    """Bounds on the decay sweep that retires unused provisional beliefs."""

    floor_confidence: float = Field(default=0.2, gt=0.0, le=1.0)
    step: float = Field(default=0.05, gt=0.0, le=1.0)
    max_per_sweep: int = Field(default=200, ge=1)


class FormationProfile(_ProfileModel):
    """When consolidation runs and which formation behaviors are enabled."""

    session_boundary_enabled: bool = True
    scheduled_enabled: bool = True
    scheduled_interval_seconds: int = Field(default=86_400, ge=1)
    established_facts_enabled: bool = True
    decay: DecayProfile = Field(default_factory=DecayProfile)


class SnapshotProfile(_ProfileModel):
    """One session class's opening memory-snapshot ceiling."""

    max_items: int = Field(default=80, ge=1)
    max_tokens: int = Field(default=3_000, ge=1)
    max_window_ratio: float = Field(default=0.02, gt=0.0, le=1.0)


class SnapshotProfiles(_ProfileModel):
    """Snapshot ceilings for the run kinds `context/plan.yaml` does not size.

    The interactive ceiling is not here: the planner already reads it from
    `context/plan.yaml`, and two sources for one number is a bug waiting for an
    overlay.
    """

    async_: SnapshotProfile = Field(
        default_factory=lambda: SnapshotProfile(max_items=80, max_tokens=3_000),
        alias="async",
    )
    child: SnapshotProfile = Field(
        default_factory=lambda: SnapshotProfile(max_items=15, max_tokens=500),
    )


class TraceProfile(_ProfileModel):
    """How long the operator tier of a recall trace survives."""

    operator_retention_days: int = Field(default=30, ge=1)


class MemoryProfiles(_ProfileModel):
    """The whole validated `memory/profiles.yaml` document."""

    schema_version: Literal[1] = 1
    retrieval: RetrievalProfile = Field(default_factory=RetrievalProfile)
    formation: FormationProfile = Field(default_factory=FormationProfile)
    snapshots: SnapshotProfiles = Field(default_factory=SnapshotProfiles)
    traces: TraceProfile = Field(default_factory=TraceProfile)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> MemoryProfiles:
        """Validate a loaded document, naming the file an operator would edit."""

        try:
            return cls.model_validate(document)
        except ValidationError as exc:
            raise ConfigurationError(f"{MEMORY_PROFILE_DOCUMENT} is invalid: {exc}") from exc


DEFAULT_RETRIEVAL_PROFILE = RetrievalProfile()
DEFAULT_FORMATION_PROFILE = FormationProfile()
DEFAULT_TRACE_PROFILE = TraceProfile()
