"""Inbound messaging surface domain values shared by channel adapters."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from agent_core.domain.devices import PushProvider
from agent_core.domain.sessions import SessionStatus

PAIRING_CODE_BYTES = 8
PAIRING_SALT_BYTES = 16
SURFACE_EXTERNAL_KEY_MAX_LENGTH = 255


def _aware_utc(value: datetime, subject: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{subject} must be aware")
    return value.astimezone(UTC)


def _hash_pairing_code(code: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        code.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )


class InboundDisposition(StrEnum):
    SUBMITTED = "submitted"
    INPUT_DELIVERED = "input_delivered"
    COMMAND_HANDLED = "command_handled"
    REJECTED_UNPAIRED = "rejected_unpaired"
    REJECTED_LOCKED = "rejected_locked"
    REJECTED_RATE = "rejected_rate"
    REJECTED_ACTIVE_RUN = "rejected_active_run"
    REJECTED_ADMISSION = "rejected_admission"
    IGNORED_MEDIA = "ignored_media"
    IGNORED_CHAT_KIND = "ignored_chat_kind"


class SessionRotationReason(StrEnum):
    EXPLICIT = "explicit"
    IDLE = "idle"
    SESSION_CLOSED = "session_closed"
    SESSION_MISSING = "session_missing"
    AGENT_VERSION_CHANGED = "agent_version_changed"
    PAIRING_REVOKED = "pairing_revoked"


class SurfaceReplyStatus(StrEnum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    FAILED = "failed"


class SurfaceChatKind(StrEnum):
    DIRECT = "direct"
    GROUP = "group"
    THREAD = "thread"


class SurfaceMessageKind(StrEnum):
    TEXT = "text"
    MEDIA = "media"


class SurfaceTransportOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRY = "retry"
    REJECTED = "rejected"


class SurfaceAdmissionDecision(BaseModel):
    """Closed, content-free outcome from the tenant admission boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> Self:
        if self.allowed == (self.reason_code is not None):
            raise ValueError("surface admission decision has inconsistent reason")
        return self


class SurfaceLimits(BaseModel):
    """Versioned inbound, session, and delivery limits for messaging surfaces."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    poll_timeout_seconds: int = Field(gt=0, le=50)
    session_idle_seconds: int = Field(gt=0)
    code_expiry_seconds: int = Field(gt=0)
    code_max_attempts: int = Field(gt=0)
    lockout_seconds: int = Field(gt=0)
    per_sender_messages_per_minute: int = Field(gt=0)
    max_active_runs_per_tenant: int = Field(gt=0)
    daily_cost: Decimal = Field(gt=0)
    monthly_cost: Decimal = Field(gt=0)
    inbound_text_max_chars: int = Field(gt=0, le=1_000_000)
    chunk_size: int = Field(gt=0, le=4096)
    claim_batch: int = Field(gt=0)
    lease_seconds: int = Field(gt=0)
    fallback_poll_seconds: float = Field(gt=0)
    retry_delays_seconds: tuple[float, ...] = Field(min_length=1)

    @field_validator("retry_delays_seconds")
    @classmethod
    def retry_delays_are_positive(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(delay <= 0 for delay in value):
            raise ValueError("surface retry delays must be positive")
        return value

    @model_validator(mode="after")
    def cost_windows_are_ordered(self) -> Self:
        if self.monthly_cost < self.daily_cost:
            raise ValueError("surface monthly cost must cover the daily cost")
        return self


class SurfaceInboundMessage(BaseModel):
    """Channel-neutral update admitted only after transport authentication."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_id: UUID
    provider: PushProvider
    external_update_id: str = Field(min_length=1, max_length=255)
    sender_id: str = Field(min_length=1, max_length=255)
    sender_label: str | None = Field(default=None, min_length=1, max_length=255)
    chat_ref: str = Field(min_length=1, max_length=255)
    chat_kind: SurfaceChatKind
    message_kind: SurfaceMessageKind
    text: str | None = Field(default=None, max_length=1_000_000)
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def received_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value, "surface update received_at")

    @model_validator(mode="after")
    def content_matches_kind(self) -> Self:
        if self.message_kind is SurfaceMessageKind.TEXT:
            if self.text is None or not self.text.strip():
                raise ValueError("text surface update requires non-blank text")
        elif self.text is not None:
            raise ValueError("non-text surface update cannot carry text")
        return self


class SurfaceTransportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: SurfaceTransportOutcome
    external_message_id: str | None = Field(default=None, min_length=1, max_length=255)
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> Self:
        if self.outcome is SurfaceTransportOutcome.DELIVERED and self.reason_code is not None:
            raise ValueError("delivered transport result cannot carry a reason")
        if self.outcome is not SurfaceTransportOutcome.DELIVERED and self.reason_code is None:
            raise ValueError("failed transport result requires a reason")
        return self


class PairingCode(BaseModel):
    """Stored half of a one-time pairing code; plaintext is never retained."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    surface_id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    code_hash: bytes = Field(min_length=32, max_length=32)
    code_salt: bytes = Field(min_length=PAIRING_SALT_BYTES)
    granted_scopes: frozenset[str]
    label: str | None = Field(default=None, min_length=1, max_length=255)
    expires_at: datetime
    max_attempts: int = Field(gt=0)
    attempts: int = Field(default=0, ge=0)
    consumed_at: datetime | None = None
    created_by_principal_id: str = Field(min_length=1, max_length=255)
    created_at: datetime

    @field_validator("expires_at", "consumed_at", "created_at")
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware_utc(value, "pairing code instant")

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("pairing code expiry must follow creation")
        if self.attempts > self.max_attempts:
            raise ValueError("attempts cannot exceed max_attempts")
        if self.consumed_at is not None and self.consumed_at < self.created_at:
            raise ValueError("pairing code consumption precedes creation")
        return self


class IssuedPairingCode(BaseModel):
    """Single-presentation response returned to the authenticated minter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: PairingCode
    code: SecretStr


class Pairing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    surface_id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    sender_id: str = Field(min_length=1, max_length=255)
    sender_label: str | None = Field(default=None, min_length=1, max_length=255)
    granted_scopes: frozenset[str]
    paired_at: datetime
    revoked_at: datetime | None = None
    revoked_by: str | None = Field(default=None, min_length=1, max_length=255)
    last_message_at: datetime | None = None

    @field_validator("paired_at", "revoked_at", "last_message_at")
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware_utc(value, "pairing instant")

    @model_validator(mode="after")
    def revocation_is_consistent(self) -> Self:
        if (self.revoked_at is None) != (self.revoked_by is None):
            raise ValueError("revocation time and actor must be present together")
        if self.revoked_at is not None and self.revoked_at < self.paired_at:
            raise ValueError("pairing revocation precedes pairing")
        if self.last_message_at is not None and self.last_message_at < self.paired_at:
            raise ValueError("last message precedes pairing")
        return self


class SenderLockout(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_id: UUID
    sender_id: str = Field(min_length=1, max_length=255)
    failed_attempts: int = Field(ge=0)
    window_started_at: datetime
    locked_until: datetime | None = None

    @field_validator("window_started_at", "locked_until")
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware_utc(value, "sender lockout instant")


class SurfaceSession(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    surface_id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    external_key: str = Field(min_length=4, max_length=SURFACE_EXTERNAL_KEY_MAX_LENGTH)
    session_id: UUID
    created_at: datetime
    last_inbound_at: datetime | None = None
    rotated_at: datetime | None = None

    @field_validator("created_at", "last_inbound_at", "rotated_at")
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware_utc(value, "surface session instant")

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> Self:
        if not self.external_key.startswith("dm:"):
            raise ValueError("only direct-message surface keys are supported")
        if self.rotated_at is not None and self.rotated_at < self.created_at:
            raise ValueError("surface session rotation precedes creation")
        if self.last_inbound_at is not None and self.last_inbound_at < self.created_at:
            raise ValueError("surface session inbound time precedes creation")
        return self


class InboundReceipt(BaseModel):
    """Content-free inbound idempotency boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface_id: UUID
    external_update_id: str = Field(min_length=1, max_length=255)
    received_at: datetime
    disposition: InboundDisposition
    session_id: UUID | None = None
    run_id: UUID | None = None
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("received_at")
    @classmethod
    def received_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value, "receipt received_at")


class SurfaceReply(BaseModel):
    """Durable reply work item; content is derived from the owned run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    surface_id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    run_id: UUID
    chat_ref: str = Field(min_length=1, max_length=255)
    status: SurfaceReplyStatus = SurfaceReplyStatus.PENDING
    chunks_total: int | None = Field(default=None, ge=0)
    chunks_sent: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime
    claimed_by: str | None = Field(default=None, min_length=1, max_length=255)
    claimed_until: datetime | None = None
    created_at: datetime
    settled_at: datetime | None = None

    @field_validator("next_attempt_at", "claimed_until", "created_at", "settled_at")
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware_utc(value, "surface reply instant")

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> Self:
        if (self.claimed_by is None) != (self.claimed_until is None):
            raise ValueError("reply claim owner and expiry must be present together")
        if self.chunks_total is not None and self.chunks_sent > self.chunks_total:
            raise ValueError("sent chunks cannot exceed total chunks")
        if self.status is SurfaceReplyStatus.PENDING and self.settled_at is not None:
            raise ValueError("pending reply cannot be settled")
        if self.status is not SurfaceReplyStatus.PENDING and self.settled_at is None:
            raise ValueError("settled reply requires settled_at")
        return self


def issue_pairing_code(
    *,
    pairing_id: UUID,
    surface_id: UUID,
    tenant_id: str,
    principal_id: str,
    created_by_principal_id: str,
    granted_scopes: frozenset[str],
    label: str | None,
    now: datetime,
    expires_after: timedelta,
    max_attempts: int,
    code: str | None = None,
    salt: bytes | None = None,
) -> IssuedPairingCode:
    instant = _aware_utc(now, "pairing code creation")
    if expires_after <= timedelta(0):
        raise ValueError("pairing code lifetime must be positive")
    plaintext = secrets.token_urlsafe(PAIRING_CODE_BYTES) if code is None else code
    if len(plaintext.encode("utf-8")) < PAIRING_CODE_BYTES:
        raise ValueError("pairing code must contain at least forty bits of entropy")
    effective_salt = secrets.token_bytes(PAIRING_SALT_BYTES) if salt is None else salt
    record = PairingCode(
        id=pairing_id,
        surface_id=surface_id,
        tenant_id=tenant_id,
        principal_id=principal_id,
        code_hash=_hash_pairing_code(plaintext, effective_salt),
        code_salt=effective_salt,
        granted_scopes=granted_scopes,
        label=label,
        expires_at=instant + expires_after,
        max_attempts=max_attempts,
        created_by_principal_id=created_by_principal_id,
        created_at=instant,
    )
    return IssuedPairingCode(record=record, code=SecretStr(plaintext))


def pairing_code_matches(record: PairingCode, candidate: str) -> bool:
    """Compare against the salted record without an early-return prefix check."""

    candidate_hash = _hash_pairing_code(candidate, record.code_salt)
    return hmac.compare_digest(record.code_hash, candidate_hash)


def direct_message_key(chat_ref: str) -> str:
    if not chat_ref or chat_ref != chat_ref.strip() or ":" in chat_ref:
        raise ValueError("surface chat reference is invalid")
    if any(ord(character) < 33 or ord(character) > 126 for character in chat_ref):
        raise ValueError("surface chat reference is invalid")
    key = f"dm:{chat_ref}"
    if len(key) > SURFACE_EXTERNAL_KEY_MAX_LENGTH:
        raise ValueError("surface external key exceeds 255 characters")
    return key


def rotation_reason(
    mapping: SurfaceSession | None,
    *,
    now: datetime,
    idle_after: timedelta,
    session_status: SessionStatus | None,
    last_message_at: datetime | None,
    mapped_agent_version: str | None,
    current_agent_version: str,
    force_new: bool = False,
    pairing_revoked: bool = False,
) -> SessionRotationReason | None:
    instant = _aware_utc(now, "surface session rotation check")
    if mapping is None or session_status is None:
        return SessionRotationReason.SESSION_MISSING if mapping is not None else None
    if mapping.rotated_at is not None:
        return SessionRotationReason.SESSION_MISSING
    if pairing_revoked:
        return SessionRotationReason.PAIRING_REVOKED
    if force_new:
        return SessionRotationReason.EXPLICIT
    if session_status is SessionStatus.CLOSED:
        return SessionRotationReason.SESSION_CLOSED
    if (
        last_message_at is not None
        and instant - _aware_utc(last_message_at, "surface last message") >= idle_after
    ):
        return SessionRotationReason.IDLE
    if mapped_agent_version != current_agent_version:
        return SessionRotationReason.AGENT_VERSION_CHANGED
    return None


def rotate_surface_session(mapping: SurfaceSession, at: datetime) -> SurfaceSession:
    if mapping.rotated_at is not None:
        raise ValueError("surface session is already rotated")
    return mapping.model_copy(update={"rotated_at": _aware_utc(at, "surface rotation")})


def chunk_surface_text(text: str, *, limit: int = 4096) -> tuple[str, ...]:
    """Split exactly at paragraph/line boundaries when possible, then hard-bound."""

    if limit <= 0:
        raise ValueError("surface chunk limit must be positive")
    if not text:
        return ()
    chunks: list[str] = []
    remainder = text
    while len(remainder) > limit:
        cut = remainder.rfind("\n\n", 0, limit + 1)
        if cut >= 0:
            cut += 2
        else:
            cut = remainder.rfind("\n", 0, limit + 1)
            cut = cut + 1 if cut >= 0 else limit
        chunks.append(remainder[:cut])
        remainder = remainder[cut:]
    if remainder:
        chunks.append(remainder)
    return tuple(chunks)
