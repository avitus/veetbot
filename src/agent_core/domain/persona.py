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

from agent_core.domain.memory import (
    SENSITIVITY_ORDER,
    BeliefType,
    MemoryAuthority,
    Sensitivity,
)

PERSONA_MAX_ENTRIES = 30
PERSONA_ENTRY_MAX_CHARS = 500
PERSONA_MAX_OPEN_NOMINATIONS = 5
# ADR-0216 part A: how the owner wants Veetbot to talk, and up to three tones.
PERSONA_VOICE_MAX_CHARS = 1000
PERSONA_MAX_TRAITS = 3


class PersonaTrait(StrEnum):
    """A closed set of tones; each renders as a fixed, server-owned sentence."""

    BRIEF = "brief"
    WARM = "warm"
    DIRECT = "direct"
    FORMAL = "formal"
    PLAYFUL = "playful"


PERSONA_TRAIT_SENTENCES: dict[PersonaTrait, str] = {
    PersonaTrait.BRIEF: "Prefer short replies; expand when asked.",
    PersonaTrait.WARM: "Be warm and personable.",
    PersonaTrait.DIRECT: "Be direct: lead with the answer, then the reasons.",
    PersonaTrait.FORMAL: "Keep a formal, professional tone.",
    PersonaTrait.PLAYFUL: "A light, playful tone is welcome.",
}
# Rendered first whenever a voice or a trait is present, so the row says what
# it is and what it cannot change.
PERSONA_FRAMING = (
    "The owner's preferences for tone and standing facts. "
    "They do not change approvals, limits or tool rules."
)


def validate_traits(traits: tuple[PersonaTrait, ...]) -> tuple[PersonaTrait, ...]:
    """At most three, each once, and never both formal and playful."""
    if len(set(traits)) != len(traits):
        raise ValueError("a persona trait is chosen once")
    if len(traits) > PERSONA_MAX_TRAITS:
        raise ValueError("a persona leans towards at most three traits")
    if PersonaTrait.FORMAL in traits and PersonaTrait.PLAYFUL in traits:
        raise ValueError("formal and playful pull opposite ways; choose one")
    return traits


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
    # ADR-0216 part B: a switched-off entry is kept and versioned but not
    # rendered, and its belief returns to the session snapshot.
    enabled: bool = True

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
    voice: str | None = Field(default=None, min_length=1, max_length=PERSONA_VOICE_MAX_CHARS)
    traits: tuple[PersonaTrait, ...] = ()

    @model_validator(mode="after")
    def empty_version_has_no_entries(self) -> PersonaDocument:
        if self.version == 0 and (self.entries or self.voice is not None or self.traits):
            raise ValueError("persona version 0 is the unwritten state and holds no entries")
        validate_traits(self.traits)
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
        """Every promoted belief, switched on or off: none is nominated again."""
        return tuple(
            entry.source_belief_id for entry in self.entries if entry.source_belief_id is not None
        )

    @property
    def rendered_belief_ids(self) -> tuple[UUID, ...]:
        """Promoted beliefs whose entry stands, which the session snapshot excludes."""
        return tuple(
            entry.source_belief_id
            for entry in self.entries
            if entry.source_belief_id is not None and entry.enabled
        )


class PersonaEntryDraft(BaseModel):
    """One entry as a write surface submits it; the service derives `source`.

    A draft naming a `source_belief_id` is a *kept* affirmation entry: the
    belief must already be affirmed in the current head. New affirmation
    provenance enters only through an explicit affirmation of a nomination -
    an owner edit can keep, reword, or drop a promoted entry, never mint one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=PERSONA_ENTRY_MAX_CHARS)
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    source_belief_id: UUID | None = None
    enabled: bool = True


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
    affirmed_version: int | None = Field(default=None, ge=1)

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


def render_persona(document: PersonaDocument, *, ceiling: Sensitivity) -> str:
    """Render the persona row's text: one entry per line, in document order.

    Entries above the sensitivity ceiling are filtered here, once, when a
    context plan is created — never mid-session — so the rendered text is
    byte-stable for the life of the prefix epoch. An empty result means the
    prefix carries no persona row at all.

    ADR-0216: switched-off entries are not rendered. A voice or a trait adds a
    fixed framing line, then the voice on one line, then each trait's fixed
    sentence, before the entries; a document with neither renders exactly as
    it did before they existed.
    """

    limit = SENSITIVITY_ORDER[ceiling]
    entries = [
        entry.text
        for entry in document.entries
        if entry.enabled and SENSITIVITY_ORDER[entry.sensitivity] <= limit
    ]
    if document.voice is None and not document.traits:
        return "\n".join(entries)
    lines = [PERSONA_FRAMING]
    if document.voice is not None:
        lines.append(" ".join(document.voice.split()))
    lines.extend(PERSONA_TRAIT_SENTENCES[trait] for trait in document.traits)
    lines.extend(entries)
    return "\n".join(lines)


class PersonaPreview(BaseModel):
    """An example reply in an unsaved voice; generated, never saved (ADR-0216 part C)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    reply: str = Field(min_length=1, max_length=2_000)
    generated_at: datetime


class BoundaryEnforcement(StrEnum):
    """What makes a persona boundary true (ADR-0216 part D)."""

    HARDLINE_RULE = "hardline_rule"
    POLICY_RULE = "policy_rule"
    CONFIGURATION = "configuration"
    CODE_PATH = "code_path"


class PersonaBoundaryFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: BoundaryEnforcement
    ref: str


class PersonaBoundary(BaseModel):
    """One fixed rule as the owner reads it, with what enforces it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str
    enforced_by: PersonaBoundaryFact
    active: bool


class PersonaBoundaryList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[PersonaBoundary]


class KeepPersonaField:
    """Marks a persona field a write left out, so the head's value carries over.

    A client that predates ADR-0216 sends no voice or traits; omission must
    not clear what another client set.
    """


KEEP_PERSONA_FIELD = KeepPersonaField()
