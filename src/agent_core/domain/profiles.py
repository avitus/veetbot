"""The base model behind every `profiles.yaml` configuration document."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FrozenProfileModel(BaseModel):
    """Reject unknown knobs and refuse mutation after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
