"""Pure value contracts for attributed, bounded email semantic evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_core.domain.memory import BeliefType


class EmailSemanticValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EmailSemanticSource(EmailSemanticValue):
    account_id: str = Field(min_length=1, max_length=256)
    provider_thread_id: str = Field(min_length=1, max_length=1024)
    message_id: str = Field(min_length=1, max_length=1024)
    session_id: UUID
    source_event_sequence: int = Field(ge=1)
    header_event_sequence: int | None = Field(default=None, ge=1)
    header_session_id: UUID | None = None
    body_offset: int = Field(default=0, ge=0)
    tool_name: str = Field(min_length=1, max_length=256)
    sender: str = Field(min_length=1, max_length=8192)
    body: str = Field(min_length=1, max_length=65536)
    sent_at: datetime

    @model_validator(mode="after")
    def aware_date(self) -> EmailSemanticSource:
        if self.sent_at.tzinfo is None or self.sent_at.utcoffset() is None:
            raise ValueError("email evidence date must be timezone-aware")
        return self


class EmailSemanticFact(EmailSemanticValue):
    message_id: str = Field(min_length=1, max_length=1024)
    quote: str = Field(min_length=1, max_length=2048)
    belief_type: Literal[BeliefType.FACT, BeliefType.RELATIONSHIP, BeliefType.PREFERENCE]
    subject: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=1024)
    confidence: float = Field(default=0.4, ge=0, le=1)


class EmailSemanticProposal(EmailSemanticValue):
    """Optional bounded field on the ordinary metered assessment response."""

    semantic_facts: list[EmailSemanticFact] = Field(default_factory=list, max_length=20)


def semantic_source_key(account_id: str, thread_id: str, message_id: str) -> str:
    return hashlib.sha256(json.dumps([account_id, thread_id, message_id]).encode()).hexdigest()
