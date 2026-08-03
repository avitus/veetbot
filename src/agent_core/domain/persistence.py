"""Durable-persistence values that cross repository boundaries."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from agent_core.domain.messages import ConversationItem, CostSource, ModelUsage, StopReason
from agent_core.domain.runs import Run, RunStatus

type ModelErrorKind = Literal["transient", "permanent", "protocol"]


class WorkerLease(BaseModel):
    run_id: UUID
    worker_id: str
    lease_epoch: int


class ClaimedRun(BaseModel):
    run: Run
    lease: WorkerLease


class IdempotencyRecord(BaseModel):
    key: str
    tenant_id: str
    principal_id: str
    request_hash: str
    run_id: UUID
    created_at: datetime
    expires_at: datetime


class ProjectionCursor(BaseModel):
    projection_name: str
    scope: str
    watermark_seq: int
    builder_version: str
    updated_at: datetime


class SessionHistory(BaseModel):
    session_id: UUID
    through_sequence: int
    items: list[ConversationItem] = Field(default_factory=list)
    builder_version: str


class TrajectoryProjection(BaseModel):
    run_id: UUID
    first_sequence: int
    last_sequence: int
    terminal: bool
    builder_version: str
    updated_at: datetime


class ModelCallRecord(BaseModel):
    attempt_id: UUID
    run_id: UUID
    session_id: UUID
    tenant_id: str
    step_number: int
    attempt_number: int
    provider: str
    model: str
    model_policy: str
    registry_version: str
    prefix_sha256: str
    usage: ModelUsage
    cost: Decimal
    cost_source: CostSource
    price_id: str | None = None
    stop_reason: StopReason | None = None
    error_kind: ModelErrorKind | None = None
    started_at: datetime
    finished_at: datetime | None = None


class UsageRollup(BaseModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    cost: Decimal = Decimal("0")


class ReclaimedRun(BaseModel):
    run_id: UUID
    session_id: UUID
    previous_epoch: int
    status: RunStatus
