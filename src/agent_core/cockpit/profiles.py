"""Frozen models behind the `cockpit/profiles.yaml` configuration document.

The shipped document is the defaults: every field below repeats the value the
document ships, and a static test pins the two together. The document grows
with the knobs each Cockpit build step reads (docs/plan/cockpit.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_core.config import ConfigurationError

COCKPIT_PROFILE_DOCUMENT = "cockpit/profiles.yaml"


class _ProfileModel(BaseModel):
    """Reject unknown knobs and refuse mutation after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CockpitPublicationProfile(_ProfileModel):
    """How often widgets may be committed and how often the branch is imported."""

    commits_per_widget_per_hour: int = Field(default=12, ge=1)
    commits_per_principal_per_hour: int = Field(default=60, ge=1)
    import_interval_seconds: int = Field(default=60, ge=10)

    @model_validator(mode="after")
    def principal_covers_one_widget(self) -> CockpitPublicationProfile:
        if self.commits_per_principal_per_hour < self.commits_per_widget_per_hour:
            raise ValueError(
                "commits_per_principal_per_hour must be at least commits_per_widget_per_hour"
            )
        return self


class CockpitProfiles(_ProfileModel):
    """The whole `cockpit/profiles.yaml` document."""

    schema_version: Literal[1] = 1
    publication: CockpitPublicationProfile = Field(default_factory=CockpitPublicationProfile)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> CockpitProfiles:
        """Validate a loaded document, naming the file an operator would edit."""

        try:
            return cls.model_validate(document)
        except ValidationError as exc:
            raise ConfigurationError(f"{COCKPIT_PROFILE_DOCUMENT} is invalid: {exc}") from exc
