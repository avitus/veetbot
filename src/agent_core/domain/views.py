"""Public application views shared by the CLI and HTTP surface."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.devices import (
    DeviceInvocationStatus,
    DeviceKind,
    DeviceStatus,
    PushEnvironment,
    PushProvider,
)
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Polarity,
    Portability,
    Sensitivity,
)
from agent_core.domain.notifications import (
    Notification,
    NotificationDelivery,
    NotificationKind,
)
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


class DeviceView(BaseModel):
    id: UUID
    client_device_id: str
    name: str
    kind: DeviceKind
    platform: str
    app_bundle_id: str | None
    push_provider: PushProvider | None
    push_environment: PushEnvironment | None
    push_token_fingerprint: str | None
    push_token_updated_at: datetime | None
    push_token_invalidated_at: datetime | None
    muted_kinds: frozenset[NotificationKind]
    capabilities: frozenset[str]
    status: DeviceStatus
    revoked_at: datetime | None
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime


class DeviceRegistrationResult(BaseModel):
    device: DeviceView
    replayed: bool = Field(default=False, exclude=True)


class TestNotificationResult(BaseModel):
    notification_id: UUID | None
    replayed: bool = Field(default=False, exclude=True)


class DeviceInvocationView(BaseModel):
    """One pending device-scoped call, as the device's fetch route returns it."""

    id: UUID
    tool_name: str
    arguments: dict[str, Any]
    created_at: datetime
    expires_at: datetime


class DeviceInvocationList(BaseModel):
    """Everything one device still owes an answer for, oldest first.

    A device's pending queue is bounded by the invocation timeout rather than
    by a page size, so this is a whole answer rather than a keyset page.
    """

    invocations: list[DeviceInvocationView]


class DeviceInvocationResultView(BaseModel):
    """The recorded terminal state of one device-scoped call."""

    id: UUID
    status: DeviceInvocationStatus
    resolved_at: datetime | None


class DeviceIngestResult(BaseModel):
    """Where one ingested device message was routed, and whether it was a replay."""

    duplicate: bool
    session_id: UUID
    run_id: UUID


class NotificationInboxItem(BaseModel):
    notification: Notification
    deliveries: list[NotificationDelivery]


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


class MemoryView(BaseModel):
    """The public projection of a belief: the spec's exposure list, exactly.

    Built by an explicit allow-list rather than by excluding fields from
    `MemoryRecord`, so a field added later to the record does not leak here
    by omission. `tenant_id`, `principal_id`, `utility`, and `store_position`
    are withheld and do not exist on this model at all. `formation_run_id`,
    `consolidation_policy_version`, and `origin_scopes` are exposed by owner
    decision (docs/status/questions-for-review.md, Milestone 17 section,
    2026-08-23).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    subject: str
    statement: str
    belief_type: BeliefType
    status: MemoryStatus
    polarity: Polarity
    scope: str
    portability: Portability
    authority: MemoryAuthority
    sensitivity: Sensitivity
    confidence: float
    corroboration_count: int
    flagged_for_review: bool
    conflicts_with: list[UUID]
    superseded_by: UUID | None
    source_session_id: UUID
    source_event_ids: list[int]
    formation_run_id: UUID
    consolidation_policy_version: str
    origin_scopes: list[str]
    valid_from: datetime
    valid_to: datetime | None
    expires_at: datetime | None
    last_reinforced_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: MemoryRecord) -> MemoryView:
        return cls(
            id=record.id,
            subject=record.subject,
            statement=record.statement,
            belief_type=record.belief_type,
            status=record.status,
            polarity=record.polarity,
            scope=record.scope,
            portability=record.portability,
            authority=record.authority,
            sensitivity=record.sensitivity,
            confidence=record.confidence,
            corroboration_count=record.corroboration_count,
            flagged_for_review=record.flagged_for_review,
            conflicts_with=list(record.conflicts_with),
            superseded_by=record.superseded_by,
            source_session_id=record.source_session_id,
            source_event_ids=list(record.source_event_ids),
            formation_run_id=record.formation_run_id,
            consolidation_policy_version=record.consolidation_policy_version,
            origin_scopes=list(record.origin_scopes),
            valid_from=record.valid_from,
            valid_to=record.valid_to,
            expires_at=record.expires_at,
            last_reinforced_at=record.last_reinforced_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


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
