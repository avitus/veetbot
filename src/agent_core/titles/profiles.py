"""Frozen models behind the `titles/profiles.yaml` configuration document.

The shipped document is the defaults: every field below repeats the value the
document ships, so a composition with no operator overlay behaves exactly as
the document says, and a static test pins the two together.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_core.config import ConfigurationError

TITLE_PROFILE_DOCUMENT = "titles/profiles.yaml"


class _ProfileModel(BaseModel):
    """Reject unknown knobs and refuse mutation after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TitleGenerationProfile(_ProfileModel):
    """Whether the title pass runs, on which model, and how much it reads."""

    enabled: bool = True
    model_policy: str = Field(default="balanced", pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    batch_size: int = Field(default=4, ge=1, le=32)
    recent_messages: int = Field(default=3, ge=1, le=10)
    message_chars: int = Field(default=400, ge=40, le=2_000)


class TitleProfiles(_ProfileModel):
    """The whole `titles/profiles.yaml` document."""

    schema_version: Literal[1] = 1
    generation: TitleGenerationProfile = Field(default_factory=TitleGenerationProfile)

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> TitleProfiles:
        """Validate a loaded document, naming the file an operator would edit."""

        try:
            return cls.model_validate(document)
        except ValidationError as exc:
            raise ConfigurationError(f"{TITLE_PROFILE_DOCUMENT} is invalid: {exc}") from exc
