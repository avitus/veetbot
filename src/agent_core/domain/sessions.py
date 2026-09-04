"""Conversation-session domain objects."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

SESSION_TITLE_MAX_LENGTH = 64
SESSION_BROWSER_PROFILE_METADATA_KEY = "browser_profile_id"
SESSION_SCHEDULE_ID_METADATA_KEY = "schedule_id"
SESSION_RUN_KIND_METADATA_KEY = "run_kind"
SESSION_PROJECT_SCOPE_METADATA_KEY = "project_scope"
SESSION_RESERVED_METADATA_KEYS = frozenset(
    {
        SESSION_BROWSER_PROFILE_METADATA_KEY,
        SESSION_SCHEDULE_ID_METADATA_KEY,
        SESSION_RUN_KIND_METADATA_KEY,
    }
)
DEFAULT_PROJECT_SCOPE = "general"


def project_scope(metadata: Mapping[str, Any]) -> str:
    """Name the project a session belongs to, falling back to the general one.

    Memory is scoped by project: consolidation records the scope a belief was
    learned in and recall demotes beliefs carried in from another one, so every
    path that consolidates or recalls reads the scope from the same place.
    """

    scope = metadata.get(SESSION_PROJECT_SCOPE_METADATA_KEY)
    if isinstance(scope, str) and scope.strip():
        return scope.strip()
    return DEFAULT_PROJECT_SCOPE


def conversation_title(text: str) -> str | None:
    """Derive the stable sidebar title used by every client."""

    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    return collapsed[:SESSION_TITLE_MAX_LENGTH]


class SessionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class Session(BaseModel):
    id: UUID
    tenant_id: str
    principal_id: str
    agent_id: UUID
    agent_version: str
    status: SessionStatus
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class SessionCursor(BaseModel):
    """Repository-level keyset cursor after API decoding."""

    updated_at: datetime
    id: UUID
