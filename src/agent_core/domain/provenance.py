"""Typed provenance pointers shared by checkpoints and context compaction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.policies import TrustLevel


class ElidedSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    trust_level: TrustLevel
    byte_length: int = Field(ge=0)
    artifact_ref: str | None = None
    event_id: int = Field(gt=0)
