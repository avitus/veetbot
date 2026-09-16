"""Chat thread folder, membership, and proposal value types (Milestone 29).

A folder is owner-organized, principal-scoped session state: a name the
owner gave, unique on its case-folded key, holding chat conversations. A
proposal is a grouping the maintenance pass suggests and only the owner
resolves; it copies the member identifiers and the name it proposes so it
stays self-contained, and it is keyed on the grouping itself so the owner's
verdict survives re-derivation.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from itertools import pairwise
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.errors import FolderNameError
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.sessions import (
    SESSION_EMAIL_OPERATIONAL_METADATA_KEY,
    SESSION_EMAIL_THREAD_ID_METADATA_KEY,
    SESSION_RUN_KIND_METADATA_KEY,
    SESSION_SCHEDULE_ID_METADATA_KEY,
)

FOLDER_NAME_MAX_LENGTH = 64
FOLDER_MAX_PER_PRINCIPAL = 200
FOLDER_PROPOSAL_MAX_MEMBERS = 12
FOLDER_PROPOSAL_RATIONALE_MAX_CHARS = 200
# Sessions the platform already marks as email, operational, scheduled, or
# delegated are never chat conversations, whatever the key's value.
FOLDER_CHAT_EXCLUSION_METADATA_KEYS = frozenset(
    {
        SESSION_EMAIL_THREAD_ID_METADATA_KEY,
        SESSION_EMAIL_OPERATIONAL_METADATA_KEY,
        SESSION_SCHEDULE_ID_METADATA_KEY,
        SESSION_RUN_KIND_METADATA_KEY,
    }
)
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})


class FolderProposalKind(StrEnum):
    NEW_FOLDER = "new_folder"
    ADD_TO_FOLDER = "add_to_folder"


class FolderProposalState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class FolderProposalDerivation(StrEnum):
    LEXICAL = "lexical"
    MODEL = "model"


class FolderWithdrawalReason(StrEnum):
    MEMBER_GONE = "member_gone"
    TARGET_GONE = "target_gone"
    NAME_TAKEN = "name_taken"


def normalize_folder_name(raw: str) -> str:
    """Collapse whitespace, apply NFC, and refuse what a folder name may not be."""

    collapsed = " ".join(unicodedata.normalize("NFC", raw).split())
    if not collapsed:
        raise FolderNameError("a folder name is required")
    if len(collapsed) > FOLDER_NAME_MAX_LENGTH:
        raise FolderNameError(f"a folder name holds at most {FOLDER_NAME_MAX_LENGTH} characters")
    if any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in collapsed):
        raise FolderNameError("a folder name may not contain control characters")
    if contains_secret_material(collapsed):
        raise FolderNameError("a folder name may not carry credential-shaped material")
    if contains_injection_pattern(collapsed):
        raise FolderNameError("a folder name may not carry an instruction pattern")
    return collapsed


def folder_name_key(name: str) -> str:
    """The case-folded normalized name: the uniqueness key per principal."""

    return normalize_folder_name(name).casefold()


def proposal_content_key(
    kind: FolderProposalKind,
    target_folder_id: UUID | None,
    member_session_ids: Iterable[UUID],
) -> str:
    """Hash the grouping itself, so a verdict follows it across identities."""

    members = sorted(member_session_ids, key=lambda value: value.int)
    document = {
        "kind": FolderProposalKind(kind).value,
        "target": str(target_folder_id) if target_folder_id is not None else None,
        "members": [str(member) for member in members],
    }
    encoded = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_chat_session(metadata: Mapping[str, Any]) -> bool:
    """True when nothing in the session's metadata marks it as another kind."""

    return not any(key in metadata for key in FOLDER_CHAT_EXCLUSION_METADATA_KEYS)


class ThreadFolder(BaseModel):
    """One owner-named folder; `name` is stored already normalized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=FOLDER_NAME_MAX_LENGTH)
    created_at: datetime
    updated_at: datetime

    @field_validator("name")
    @classmethod
    def name_is_normalized(cls, value: str) -> str:
        try:
            normalized = normalize_folder_name(value)
        except FolderNameError as error:
            raise ValueError(str(error)) from error
        if normalized != value:
            raise ValueError("a folder name is stored in its normalized form")
        return value


class FolderProposal(BaseModel):
    """A grouping the maintenance pass suggested, awaiting the owner's verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    kind: FolderProposalKind
    proposed_name: str | None = Field(default=None, max_length=FOLDER_NAME_MAX_LENGTH)
    target_folder_id: UUID | None = None
    member_session_ids: tuple[UUID, ...] = Field(
        min_length=1, max_length=FOLDER_PROPOSAL_MAX_MEMBERS
    )
    rationale: str | None = Field(default=None, max_length=FOLDER_PROPOSAL_RATIONALE_MAX_CHARS)
    derivation: FolderProposalDerivation
    content_key: str = Field(default="", max_length=64)
    state: FolderProposalState = FolderProposalState.PROPOSED
    withdrawal_reason: FolderWithdrawalReason | None = None
    resulting_folder_id: UUID | None = None
    created_at: datetime
    resolved_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def derive_content_key(cls, values: Any) -> Any:
        if not isinstance(values, dict) or values.get("content_key"):
            return values
        try:
            kind = FolderProposalKind(values["kind"])
            raw_target = values.get("target_folder_id")
            target = None if raw_target is None else UUID(str(raw_target))
            members = [UUID(str(member)) for member in values["member_session_ids"]]
        except (KeyError, TypeError, ValueError):
            return values
        return {**values, "content_key": proposal_content_key(kind, target, members)}

    @field_validator("member_session_ids")
    @classmethod
    def members_are_sorted_and_unique(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        for previous, current in pairwise(value):
            if current.int <= previous.int:
                raise ValueError("proposal members are sorted and unique")
        return value

    @model_validator(mode="after")
    def kind_fixes_the_fields(self) -> FolderProposal:
        if self.kind is FolderProposalKind.NEW_FOLDER:
            if self.proposed_name is None:
                raise ValueError("a new-folder proposal names the folder it proposes")
            if self.target_folder_id is not None:
                raise ValueError("a new-folder proposal has no target folder")
        else:
            if self.target_folder_id is None:
                raise ValueError("an add-to-folder proposal names its target folder")
            if self.proposed_name is not None:
                raise ValueError("an add-to-folder proposal proposes no name")
        return self

    @model_validator(mode="after")
    def resolution_is_consistent(self) -> FolderProposal:
        resolved = self.state is not FolderProposalState.PROPOSED
        if resolved and self.resolved_at is None:
            raise ValueError("a resolved proposal records when it was resolved")
        if not resolved and self.resolved_at is not None:
            raise ValueError("an open proposal has no resolution time")
        if self.state is FolderProposalState.WITHDRAWN and self.withdrawal_reason is None:
            raise ValueError("a withdrawn proposal records why")
        if self.withdrawal_reason is not None and self.state is not FolderProposalState.WITHDRAWN:
            raise ValueError("only a withdrawn proposal carries a withdrawal reason")
        if self.state is FolderProposalState.ACCEPTED and self.resulting_folder_id is None:
            raise ValueError("an accepted proposal names the folder it filed into")
        if self.resulting_folder_id is not None and self.state is not FolderProposalState.ACCEPTED:
            raise ValueError("only an accepted proposal names a resulting folder")
        return self

    @model_validator(mode="after")
    def content_key_matches(self) -> FolderProposal:
        expected = proposal_content_key(self.kind, self.target_folder_id, self.member_session_ids)
        if self.content_key != expected:
            raise ValueError("the content key is the hash of the proposal's own grouping")
        return self
