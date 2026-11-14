"""Follow-up messages waiting for a working run to read them (ADR-0324)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.messages import ContentPart

MAX_PENDING_RUN_INPUTS = 8


class PendingRunInput(BaseModel):
    """One follow-up, held from acceptance until its run reads or drops it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str
    principal_id: str
    session_id: UUID
    run_id: UUID
    content: list[ContentPart] = Field(min_length=1)
    created_at: datetime


def follow_up_derivation(input_id: UUID) -> str:
    """Derivation key of the user message a read or promoted follow-up becomes."""

    return f"run.follow_up:{input_id}"


def follow_up_drop_derivation(input_id: UUID) -> str:
    """Derivation key of the event recording a follow-up its run never read."""

    return f"run.input.dropped:{input_id}"
