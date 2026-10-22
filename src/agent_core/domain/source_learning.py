"""Bounded proposals for owner-directed study; callers never choose authority."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SourceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    call_id: str = Field(
        min_length=1, max_length=200, description="Successful read call ID from this run."
    )
    quote: str = Field(
        min_length=8,
        max_length=2048,
        description="Exact retained excerpt; use 8-512 characters when omitting claim and subject.",
    )
    claim: str | None = Field(
        default=None,
        min_length=8,
        max_length=1024,
        description="Optional contiguous phrase from quote; omit to save the quote itself.",
    )
    subject: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="Optional subject using only words in quote; defaults to the saved phrase.",
    )
    reinforce_id: UUID | None = None


class SourceLearningArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observations: list[SourceObservation] = Field(min_length=1, max_length=32)
