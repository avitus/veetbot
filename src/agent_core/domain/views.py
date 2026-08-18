"""Public application views shared by the CLI and HTTP surface."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.runs import FailureReason, RunStatus
from agent_core.domain.sessions import SessionStatus


class TextContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class ImageContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image"] = "image"
    artifact_id: UUID
    media_type: str = Field(min_length=1)
    detail: str = "auto"


class FileContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["file"] = "file"
    artifact_id: UUID
    media_type: str = Field(min_length=1)
    filename: str | None = None


ContentBlock = Annotated[
    TextContentBlock | ImageContentBlock | FileContentBlock,
    Field(discriminator="type"),
]


class SessionView(BaseModel):
    id: UUID
    status: SessionStatus
    agent_id: str
    agent_version: str
    title: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    active_run_id: UUID | None
    last_run_id: UUID | None


class SessionMessageView(BaseModel):
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: list[ContentBlock] = Field(min_length=1)


class SubmitResult(BaseModel):
    run_id: UUID
    status: RunStatus
    replayed: bool = Field(default=False, exclude=True)


class CancelResult(BaseModel):
    run: RunView
    accepted: bool


class RunUsageView(BaseModel):
    input_tokens: int
    output_tokens: int
    cost_usd: str


class RunLimitsView(BaseModel):
    max_steps: int
    deadline_at: datetime | None
    max_cost_usd: str | None


class RunFailureView(BaseModel):
    reason: FailureReason
    message: str
    step_number: int | None
    attempt_number: int | None
    occurred_at: datetime


class RunView(BaseModel):
    id: UUID
    session_id: UUID
    parent_run_id: UUID | None
    status: RunStatus
    step_count: int
    model_call_count: int
    tool_call_count: int
    usage: RunUsageView
    limits: RunLimitsView
    failure: RunFailureView | None
    cancel_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PersistedStreamFrame(BaseModel):
    kind: Literal["persisted"] = "persisted"
    sequence: int = Field(ge=1)
    event: str
    data: dict[str, Any]


class TransientStreamFrame(BaseModel):
    kind: Literal["transient"] = "transient"
    event: str
    data: dict[str, Any]


StreamFrame = Annotated[
    PersistedStreamFrame | TransientStreamFrame,
    Field(discriminator="kind"),
]


class ApprovalFilters(BaseModel):
    status: Literal["pending"] = "pending"
    run_id: UUID | None = None
    session_id: UUID | None = None


class ApprovalView(BaseModel):
    id: UUID
    run_id: UUID
    session_id: UUID
    status: str
    tool_name: str | None
    action_summary: str
    arguments: dict[str, Any]
    risk: str
    policy_reason: str
    expires_at: datetime | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
    decision: ApprovalResolutionType | None


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class ArtifactView(BaseModel):
    id: UUID
    session_id: UUID
    run_id: UUID
    name: str
    media_type: str
    sha256: str
    size_bytes: int
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """A reopenable content handle; API code never needs a storage URI."""

    artifact: ArtifactView
    open: Callable[[], Awaitable[AsyncIterator[bytes]]]
