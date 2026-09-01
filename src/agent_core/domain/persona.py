"""Persona document and nomination value types (Milestone 22).

The persona is the owner's standing instruction text: an ordered list of
entries rendered into the frozen context prefix at trusted-configuration
trust. Every entry was either typed by the owner or explicitly affirmed from
a named belief; nothing automatic writes one. The document is versioned as a
whole and guarded by an expected-version precondition.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.memory import BeliefType, MemoryAuthority, Sensitivity

PERSONA_MAX_ENTRIES = 30
PERSONA_ENTRY_MAX_CHARS = 500
PERSONA_MAX_OPEN_NOMINATIONS = 5


class PersonaEntrySource(StrEnum):
    USER_EDIT = "user_edit"
    AFFIRMATION = "affirmation"


class PersonaNominationState(StrEnum):
    NOMINATED = "nominated"
    AFFIRMED = "affirmed"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class PersonaEntry(BaseModel):
    """One belief or standing truth, with the provenance that put it here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=PERSONA_ENTRY_MAX_CHARS)
    source: PersonaEntrySource
    source_belief_id: UUID | None = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL

    @model_validator(mode="after")
    def provenance_matches_source(self) -> PersonaEntry:
        if self.source is PersonaEntrySource.AFFIRMATION and self.source_belief_id is None:
            raise ValueError("an affirmed persona entry names its source belief")
        if self.source is PersonaEntrySource.USER_EDIT and self.source_belief_id is not None:
            raise ValueError("an owner-typed persona entry carries no source belief")
        return self


class PersonaDocument(BaseModel):
    """One immutable version of a principal's persona.

    Version 0 is the unwritten persona — a real, readable, empty state, never
    persisted. Every persisted version is dense and monotonic from 1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    version: int = Field(ge=0)
    entries: tuple[PersonaEntry, ...] = Field(max_length=PERSONA_MAX_ENTRIES)
    source: PersonaEntrySource
    source_nomination_id: UUID | None = None
    created_at: datetime

    @model_validator(mode="after")
    def empty_version_has_no_entries(self) -> PersonaDocument:
        if self.version == 0 and self.entries:
            raise ValueError("persona version 0 is the unwritten state and holds no entries")
        return self

    @classmethod
    def empty(cls, tenant_id: str, principal_id: str, now: datetime) -> PersonaDocument:
        return cls(
            tenant_id=tenant_id,
            principal_id=principal_id,
            version=0,
            entries=(),
            source=PersonaEntrySource.USER_EDIT,
            created_at=now,
        )

    @property
    def affirmed_belief_ids(self) -> tuple[UUID, ...]:
        return tuple(
            entry.source_belief_id for entry in self.entries if entry.source_belief_id is not None
        )


class PersonaNomination(BaseModel):
    """A consolidation-raised candidate awaiting the owner's verdict.

    The statement is a canonical copy taken at nomination time, so a
    nomination stays self-contained if its belief later dies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    belief_id: UUID
    statement: str = Field(min_length=1, max_length=PERSONA_ENTRY_MAX_CHARS)
    belief_type: BeliefType
    authority: MemoryAuthority
    confidence: float = Field(ge=0, le=1)
    corroboration_count: int = Field(ge=1)
    sensitivity: Sensitivity
    state: PersonaNominationState = PersonaNominationState.NOMINATED
    consolidation_run_id: UUID | None = None
    nominated_at: datetime
    resolved_at: datetime | None = None
    affirmed_version: int | None = None

    @model_validator(mode="after")
    def resolution_is_consistent(self) -> PersonaNomination:
        resolved = self.state is not PersonaNominationState.NOMINATED
        if resolved and self.resolved_at is None:
            raise ValueError("a resolved nomination records when it was resolved")
        if not resolved and self.resolved_at is not None:
            raise ValueError("an open nomination has no resolution time")
        if self.affirmed_version is not None and self.state is not PersonaNominationState.AFFIRMED:
            raise ValueError("only an affirmed nomination names the version it created")
        if self.state is PersonaNominationState.AFFIRMED and self.affirmed_version is None:
            raise ValueError("an affirmed nomination names the document version it created")
        return self
