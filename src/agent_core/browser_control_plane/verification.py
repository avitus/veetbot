"""Operator-owned sign-in definitions; never model, page, or credential input."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.browser import (
    MAXIMUM_FACT_LABEL_CHARACTERS,
    BrowserFieldKind,
    BrowserLabelSource,
    BrowserObservation,
    BrowserObservationFacts,
    normalize_browser_origin,
)
from agent_core.domain.browser_evidence import (
    BrowserRegionEvidence,
    BrowserTextEvidence,
    evidence_status,
)


class BrowserAccountControlEvidence(BaseModel):
    """An operator-owned account label on one observed non-editable control."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["control"]
    role: Literal["button", "link"]
    name: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=MAXIMUM_FACT_LABEL_CHARACTERS, repr=False)

    @field_validator("name", "text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("account control evidence must not be blank")
        return value

    def confirms(
        self, observation: BrowserObservation, facts: BrowserObservationFacts | None
    ) -> bool:
        if facts is None or facts.revision != observation.revision:
            return False
        matches = [
            element
            for element in observation.elements
            if element.role == self.role and element.name == self.name
        ]
        if len(matches) != 1:
            return False
        element = matches[0]
        detail = facts.elements.get(element.ref)
        if (
            detail is None
            or detail.field_kind is not BrowserFieldKind.NONE
            or detail.labels_truncated
        ):
            return False
        # A missing source means it was identical to the accessible name.
        label = detail.labels.get(BrowserLabelSource.VISIBLE_TEXT, element.name)
        return " ".join(label.split()) == " ".join(self.text.split())


class BrowserSiteVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: int = Field(ge=1, le=10000)
    profile_id: UUID
    origin: str
    protected_path: str = Field(pattern=r"^/[A-Za-z0-9/_~-]{0,255}$")
    ready: BrowserRegionEvidence | BrowserTextEvidence
    account: BrowserRegionEvidence | BrowserAccountControlEvidence = Field(repr=False)

    @field_validator("origin")
    @classmethod
    def exact_origin(cls, value: str) -> str:
        return normalize_browser_origin(value)

    def confirms(
        self, observation: BrowserObservation, facts: BrowserObservationFacts | None = None
    ) -> bool:
        if (
            observation.interruption is not None
            or observation.url.rstrip("/") != (self.origin + self.protected_path).rstrip("/")
            or evidence_status(observation, self.ready) != "satisfied"
        ):
            return False
        return (
            self.account.confirms(observation, facts)
            if isinstance(self.account, BrowserAccountControlEvidence)
            else evidence_status(observation, self.account) == "satisfied"
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
