"""Frozen models behind the `folders/profiles.yaml` configuration document.

The shipped document is the defaults: every field below repeats the value the
document ships, so a composition with no operator overlay behaves exactly as
the document says, and a static test pins the two together.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent_core.config import ConfigurationError

FOLDER_PROFILE_DOCUMENT = "folders/profiles.yaml"


class _ProfileModel(BaseModel):
    """Reject unknown knobs and refuse mutation after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class FolderProposalProfile(_ProfileModel):
    """When the proposal pass runs and how large a grouping must be."""

    enabled: bool = True
    threshold: int = Field(default=4, ge=2)
    max_open: int = Field(default=3, ge=1)
    interval_seconds: int = Field(default=900, ge=60)
    model_policy: str = Field(default="balanced", pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    similarity_threshold: float = Field(default=0.2, gt=0.0, le=1.0)
    max_members: int = Field(default=12, ge=2)

    @model_validator(mode="after")
    def members_cover_the_threshold(self) -> FolderProposalProfile:
        if self.max_members < self.threshold:
            raise ValueError("max_members must be at least the new-folder threshold")
        return self


class FolderProfiles(_ProfileModel):
    """The whole `folders/profiles.yaml` document."""

    schema_version: Literal[1] = 1
    proposals: FolderProposalProfile = Field(default_factory=FolderProposalProfile)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> FolderProfiles:
        """Validate a loaded document, naming the file an operator would edit."""

        try:
            return cls.model_validate(document)
        except ValidationError as exc:
            raise ConfigurationError(f"{FOLDER_PROFILE_DOCUMENT} is invalid: {exc}") from exc
