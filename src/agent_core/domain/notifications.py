"""Notification domain values and content-free push vocabulary."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import OccurrenceDisposition


class NotificationKind(StrEnum):
    APPROVAL_REQUESTED = "approval_requested"
    QUESTION_ASKED = "question_asked"
    RUN_FAILED = "run_failed"
    SCHEDULE_RUN_FINISHED = "schedule_run_finished"
    SCHEDULE_OCCURRENCE_SKIPPED = "schedule_occurrence_skipped"
    OPS_ALERT = "ops_alert"
    OPS_RECOVERED = "ops_recovered"
    TEST = "test"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    FAILED = "failed"


class DeliveryOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRY = "retry"
    UNREGISTERED = "unregistered"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class NotificationSeverity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"
    RECOVERED = "recovered"


class NotificationPayload(BaseModel):
    """Closed, content-free payload sent through a push provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    kind: NotificationKind
    title: str
    status: RunStatus | OccurrenceDisposition | None = None
    tool_name: str | None = Field(
        default=None,
        min_length=3,
        max_length=96,
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$",
    )
    session_id: UUID | None = None
    run_id: UUID | None = None
    approval_id: UUID | None = None
    question_id: UUID | None = None
    schedule_id: UUID | None = None
    occurrence_id: UUID | None = None
    notification_id: UUID
    signal: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    severity: NotificationSeverity | None = None
    reason_code: str | None = Field(
        default=None,
        min_length=5,
        max_length=128,
        pattern=r"^ops\.[a-z][a-z0-9_.]*$",
    )
    release_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )

    @model_validator(mode="after")
    def vocabulary_matches_kind(self) -> NotificationPayload:
        if self.title != NOTIFICATION_TITLES[self.kind]:
            raise ValueError("notification title must use the fixed template for its kind")

        required_identifiers = _REQUIRED_IDENTIFIERS[self.kind]
        identifiers = {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "approval_id": self.approval_id,
            "question_id": self.question_id,
            "schedule_id": self.schedule_id,
            "occurrence_id": self.occurrence_id,
        }
        present_identifiers = {name for name, value in identifiers.items() if value is not None}
        if present_identifiers != required_identifiers:
            raise ValueError("notification identifiers do not match its kind")

        allowed_statuses = _ALLOWED_SUBJECT_STATUSES[self.kind]
        if self.status not in allowed_statuses:
            raise ValueError("notification subject status does not match its kind")

        ops_values = (self.signal, self.severity, self.reason_code, self.release_id)
        if self.kind in {NotificationKind.OPS_ALERT, NotificationKind.OPS_RECOVERED}:
            if self.signal is None or self.severity is None or self.reason_code is None:
                raise ValueError("operational notification requires signal, severity, and reason")
            if self.kind is NotificationKind.OPS_ALERT:
                if self.severity is NotificationSeverity.RECOVERED:
                    raise ValueError("operational alert cannot use recovered severity")
            elif self.severity is not NotificationSeverity.RECOVERED:
                raise ValueError("operational recovery requires recovered severity")
            if self.tool_name is not None:
                raise ValueError("operational notification cannot carry a tool name")
        elif any(value is not None for value in ops_values):
            raise ValueError("only operational notifications may carry operational fields")

        if self.kind is NotificationKind.TEST and self.tool_name is not None:
            raise ValueError("test notification cannot carry a tool name")
        return self


class Notification(BaseModel):
    """One durable notification-outbox record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    kind: NotificationKind
    dedupe_key: str = Field(min_length=1, max_length=1024)
    session_id: UUID | None = None
    run_id: UUID | None = None
    approval_id: UUID | None = None
    question_id: UUID | None = None
    schedule_id: UUID | None = None
    occurrence_id: UUID | None = None
    payload: NotificationPayload
    priority: int = Field(ge=0, le=32_767)
    expires_at: datetime | None = None
    status: NotificationStatus
    attempts: int = Field(ge=0)
    next_attempt_at: datetime
    claimed_by: str | None = Field(default=None, min_length=1, max_length=255)
    claimed_until: datetime | None = None
    created_at: datetime
    settled_at: datetime | None = None

    @field_validator(
        "expires_at",
        "next_attempt_at",
        "claimed_until",
        "created_at",
        "settled_at",
    )
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return _optional_aware_utc(value)

    @model_validator(mode="after")
    def state_is_consistent(self) -> Notification:
        if self.payload.notification_id != self.id or self.payload.kind is not self.kind:
            raise ValueError("notification payload identity does not match its outbox row")
        for field_name in (
            "session_id",
            "run_id",
            "approval_id",
            "question_id",
            "schedule_id",
            "occurrence_id",
        ):
            if getattr(self.payload, field_name) != getattr(self, field_name):
                raise ValueError("notification payload references do not match its outbox row")
        if (self.claimed_by is None) != (self.claimed_until is None):
            raise ValueError("notification claim owner and expiry must be present together")
        if self.status is NotificationStatus.PENDING:
            if self.settled_at is not None:
                raise ValueError("pending notification cannot be settled")
        elif self.settled_at is None:
            raise ValueError("settled notification status requires settled_at")
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("notification expiry precedes creation")
        if self.settled_at is not None and self.settled_at < self.created_at:
            raise ValueError("notification settled_at precedes creation")
        return self


class NewNotification(BaseModel):
    """Input to the durable outbox before repository-owned delivery state exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    kind: NotificationKind
    dedupe_key: str = Field(min_length=1, max_length=1024)
    session_id: UUID | None = None
    run_id: UUID | None = None
    approval_id: UUID | None = None
    question_id: UUID | None = None
    schedule_id: UUID | None = None
    occurrence_id: UUID | None = None
    payload: NotificationPayload
    priority: int = Field(ge=0, le=32_767)
    expires_at: datetime | None = None
    next_attempt_at: datetime
    created_at: datetime

    @field_validator("expires_at", "next_attempt_at", "created_at")
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return _optional_aware_utc(value)

    @model_validator(mode="after")
    def references_match_payload(self) -> NewNotification:
        if self.payload.notification_id != self.id or self.payload.kind is not self.kind:
            raise ValueError("new notification payload identity does not match")
        for field_name in (
            "session_id",
            "run_id",
            "approval_id",
            "question_id",
            "schedule_id",
            "occurrence_id",
        ):
            if getattr(self.payload, field_name) != getattr(self, field_name):
                raise ValueError("new notification references do not match its payload")
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("new notification expiry precedes creation")
        return self


class NotificationDelivery(BaseModel):
    """One recorded transport attempt for one device target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    notification_id: UUID
    device_id: UUID
    attempt: int = Field(ge=1)
    outcome: DeliveryOutcome
    provider_reason: str | None = Field(default=None, min_length=1, max_length=128)
    provider_id: str | None = Field(default=None, min_length=1, max_length=255)
    attempted_at: datetime

    @field_validator("attempted_at")
    @classmethod
    def attempted_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class NotificationCursor(BaseModel):
    """Stable descending notification-inbox cursor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created_at: datetime
    id: UUID

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class PushMessage(BaseModel):
    """Provider-neutral message presented to a push transport."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    notification_id: UUID
    dedupe_key: str = Field(min_length=1, max_length=1024)
    payload: NotificationPayload
    priority: int = Field(ge=0, le=32_767)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def expires_at_is_aware_utc(cls, value: datetime | None) -> datetime | None:
        return _optional_aware_utc(value)

    @model_validator(mode="after")
    def payload_identity_matches(self) -> PushMessage:
        if self.payload.notification_id != self.notification_id:
            raise ValueError("push message payload identity does not match")
        return self


class PushOutcome(BaseModel):
    """Closed result returned by a push provider adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: DeliveryOutcome
    provider_reason: str | None = Field(default=None, min_length=1, max_length=128)
    provider_id: str | None = Field(default=None, min_length=1, max_length=255)


def approval_requested_key(approval_id: UUID) -> str:
    return f"approval.requested:{approval_id}"


def question_asked_key(run_id: UUID, question_id: UUID) -> str:
    return f"run.waiting_for_user:{run_id}:{question_id}"


def run_failed_key(run_id: UUID) -> str:
    return f"run.failed:{run_id}"


def schedule_run_finished_key(occurrence_id: UUID) -> str:
    return f"schedule.run_accounted:{occurrence_id}"


def schedule_occurrence_skipped_key(occurrence_id: UUID) -> str:
    return f"schedule.occurrence.skipped:{occurrence_id}"


def device_test_key(device_id: UUID, idempotency_key: str) -> str:
    if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 255:
        raise ValueError("device test idempotency key must contain 1 to 255 characters")
    return f"device.test:{device_id}:{idempotency_key}"


def ops_alert_key(tenant_id: str, signal: str, episode: int) -> str:
    _validate_ops_key_parts(tenant_id, signal, episode)
    return f"ops.{tenant_id}.{signal}.{episode}"


def ops_recovered_key(tenant_id: str, signal: str, episode: int) -> str:
    return f"{ops_alert_key(tenant_id, signal, episode)}.recovered"


NOTIFICATION_TITLES: dict[NotificationKind, str] = {
    NotificationKind.APPROVAL_REQUESTED: "Approval needed",
    NotificationKind.QUESTION_ASKED: "The agent has a question",
    NotificationKind.RUN_FAILED: "Run failed",
    NotificationKind.SCHEDULE_RUN_FINISHED: "Scheduled run finished",
    NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED: "Scheduled run skipped",
    NotificationKind.OPS_ALERT: "Production alert",
    NotificationKind.OPS_RECOVERED: "Production recovered",
    NotificationKind.TEST: "Test notification",
}

_REQUIRED_IDENTIFIERS: dict[NotificationKind, set[str]] = {
    NotificationKind.APPROVAL_REQUESTED: {"session_id", "run_id", "approval_id"},
    NotificationKind.QUESTION_ASKED: {"session_id", "run_id", "question_id"},
    NotificationKind.RUN_FAILED: {"session_id", "run_id"},
    NotificationKind.SCHEDULE_RUN_FINISHED: {
        "session_id",
        "run_id",
        "schedule_id",
        "occurrence_id",
    },
    NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED: {"schedule_id", "occurrence_id"},
    NotificationKind.OPS_ALERT: set(),
    NotificationKind.OPS_RECOVERED: set(),
    NotificationKind.TEST: set(),
}

_ALLOWED_SUBJECT_STATUSES: dict[
    NotificationKind,
    set[RunStatus | OccurrenceDisposition | None],
] = {
    NotificationKind.APPROVAL_REQUESTED: {RunStatus.WAITING_FOR_APPROVAL},
    NotificationKind.QUESTION_ASKED: {RunStatus.WAITING_FOR_USER},
    NotificationKind.RUN_FAILED: {RunStatus.FAILED},
    NotificationKind.SCHEDULE_RUN_FINISHED: {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED: {
        OccurrenceDisposition.MISSED,
        OccurrenceDisposition.SKIPPED_OVERLAP,
        OccurrenceDisposition.AUTHORIZATION_FAILED,
        OccurrenceDisposition.CONFIGURATION_FAILED,
    },
    NotificationKind.OPS_ALERT: {None},
    NotificationKind.OPS_RECOVERED: {None},
    NotificationKind.TEST: {None},
}

_OPS_SIGNAL = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_ops_key_parts(tenant_id: str, signal: str, episode: int) -> None:
    if not tenant_id or len(tenant_id) > 255:
        raise ValueError("ops tenant id must contain 1 to 255 characters")
    if len(signal) > 64 or _OPS_SIGNAL.fullmatch(signal) is None:
        raise ValueError("ops signal must be declared vocabulary")
    if episode <= 0:
        raise ValueError("ops alert episode must be positive")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification instants must be aware")
    return value.astimezone(UTC)


def _optional_aware_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _aware_utc(value)
