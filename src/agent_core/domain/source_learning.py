"""Bounded proposals for owner-directed study; callers never choose authority."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=8, max_length=2048)
    claim: str = Field(min_length=8, max_length=1024)
    subject: str = Field(min_length=1, max_length=512)
    reinforce_id: UUID | None = None


class SourceLearningArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observations: list[SourceObservation] = Field(min_length=1, max_length=32)
