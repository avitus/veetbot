"""Operator-owned sign-in definitions; never model, page, or credential input."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.browser import BrowserObservation, normalize_browser_origin
from agent_core.domain.browser_evidence import (
    BrowserRegionEvidence,
    BrowserTextEvidence,
    evidence_status,
)


class BrowserSiteVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: int = Field(ge=1, le=10000)
    profile_id: UUID
    origin: str
    protected_path: str = Field(pattern=r"^/[A-Za-z0-9/_~-]{0,255}$")
    ready: BrowserRegionEvidence | BrowserTextEvidence
    account: BrowserRegionEvidence = Field(repr=False)

    @field_validator("origin")
    @classmethod
    def exact_origin(cls, value: str) -> str:
        return normalize_browser_origin(value)

    def confirms(self, observation: BrowserObservation) -> bool:
        return (
            observation.url.rstrip("/") == (self.origin + self.protected_path).rstrip("/")
            and evidence_status(observation, self.ready) == "satisfied"
            and evidence_status(observation, self.account) == "satisfied"
        )


class BrowserVerificationCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    sites: tuple[BrowserSiteVerification, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def unique_profiles(self) -> BrowserVerificationCatalog:
        if len({site.profile_id for site in self.sites}) != len(self.sites):
            raise ValueError("one verification definition is allowed per profile")
        return self
