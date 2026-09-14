"""Principal-bound telephone correspondence and dispatch ledger values."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core.domain.security import contains_credential

MAX_CALLBACK_BYTES = 262_144


class CallConfiguration(BaseModel):
    """Only owner-reviewed public material; provider credentials live elsewhere."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    version: Literal[1] = 1
    account_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")
    phone_number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    public_name: str = Field(min_length=1, max_length=128)
    public_profile: str = Field(min_length=1, max_length=4000, repr=False)
    voice: str = Field(default="maya", min_length=1, max_length=128)
    max_duration_minutes: int = Field(default=5, ge=1, le=5)
    webhook_url: str = Field(max_length=2048)

    @field_validator("public_name", "public_profile", "voice")
    @classmethod
    def public_material(cls, value: str) -> str:
        if not value.strip() or contains_credential(value):
            raise ValueError("calling configuration contains invalid public material")
        return value

    @field_validator("webhook_url")
    @classmethod
    def public_callback(cls, value: str) -> str:
        target = urlsplit(value)
        if (
            target.scheme != "https"
            or not target.hostname
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.port not in {None, 443}
            or target.path != "/webhooks/bland"
        ):
            raise ValueError("calling webhook must be an HTTPS /webhooks/bland URL")
        return value

    @property
    def revision(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class CallRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    kind: str = Field(pattern=r"^(call|dispatch|receipt|cursor)$")
    key: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    payload: dict[str, object] = Field(repr=False)
    created_at: datetime
    updated_at: datetime

    @field_validator("payload")
    @classmethod
    def validate_retry_time(cls, value: dict[str, object]) -> dict[str, object]:
        retry_at = value.get("retry_at")
        if retry_at is None:
            return value
        try:
            if not isinstance(retry_at, str):
                raise ValueError
            parsed = datetime.fromisoformat(retry_at)
            if parsed.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise ValueError("call record retry time requires an offset-aware timestamp") from None
        return {**value, "retry_at": parsed.astimezone(UTC).isoformat()}

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("call record time requires an offset")
        return value


def call_identifier(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("invalid call identifier")
    return str(UUID(value))


class ProviderCall(BaseModel):
    """A second closed boundary before a provider result becomes durable correspondence."""

    model_config = ConfigDict(extra="forbid", strict=True)
    provider_call_id: str
    request_id: str | None
    number: str
    counterparty: str = Field(max_length=64)
    direction: Literal["inbound", "outbound"]
    status: Literal["active", "completed", "no_answer", "busy", "voicemail", "failed"]
    created_at: str
    duration_minutes: float = Field(ge=0, allow_inf_nan=False)
    summary: str = Field(max_length=4096)
    summary_complete: bool
    transcript: str = Field(max_length=16384)
    transcript_complete: bool

    @field_validator("provider_call_id", "request_id")
    @classmethod
    def valid_id(cls, value: str | None) -> str | None:
        return None if value is None else call_identifier(value)

    @field_validator("created_at")
    @classmethod
    def aware_date(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("provider call time must be aware")
        return parsed.astimezone(UTC).isoformat()
