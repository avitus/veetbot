"""Conversation-session domain objects."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


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
