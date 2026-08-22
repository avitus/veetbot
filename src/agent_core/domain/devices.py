"""Device identity domain values."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from agent_core.domain.notifications import NotificationKind


class DeviceKind(StrEnum):
    MOBILE = "mobile"
    LAPTOP = "laptop"
    DESKTOP = "desktop"
    WEB = "web"
    CLI = "cli"
    SURFACE = "surface"


class PushProvider(StrEnum):
    APNS = "apns"
    TELEGRAM = "telegram"


class PushEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class Device(BaseModel):
    """Principal-scoped device identity with secret push routing material."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    client_device_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    kind: DeviceKind
    platform: str = Field(min_length=1, max_length=64)
    app_bundle_id: str | None = Field(default=None, min_length=1, max_length=255)
    push_provider: PushProvider | None = None
    push_token: SecretStr | None = None
    push_environment: PushEnvironment | None = None
    push_token_updated_at: datetime | None = None
    push_token_invalidated_at: datetime | None = None
    muted_kinds: frozenset[NotificationKind]
    status: DeviceStatus
    revoked_at: datetime | None = None
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "push_token_updated_at",
        "push_token_invalidated_at",
        "revoked_at",
        "last_seen_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("device instants must be aware")
        return value.astimezone(UTC)

    @field_validator("push_token")
    @classmethod
    def push_token_is_not_blank(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("push token cannot be empty")
        return value

    @model_validator(mode="after")
    def push_and_status_are_consistent(self) -> Device:
        if (self.push_provider is None) != (self.push_token is None):
            raise ValueError("push provider and token must be present together")
        if self.push_provider is PushProvider.APNS:
            if self.push_environment is None:
                raise ValueError("APNs device requires a push environment")
        elif self.push_environment is not None:
            raise ValueError("only an APNs device may carry a push environment")

        if self.push_provider is PushProvider.TELEGRAM and self.kind is not DeviceKind.SURFACE:
            raise ValueError("Telegram routing belongs only to a surface device")
        if self.kind is DeviceKind.SURFACE and self.push_provider is not PushProvider.TELEGRAM:
            raise ValueError("surface device requires Telegram routing")

        if self.status is DeviceStatus.REVOKED:
            if self.revoked_at is None:
                raise ValueError("revoked device requires revoked_at")
            if self.push_token is not None or self.push_provider is not None:
                raise ValueError("revoked device cannot retain push routing")
        elif self.revoked_at is not None:
            raise ValueError("active device cannot carry revoked_at")

        if self.updated_at < self.created_at:
            raise ValueError("device updated_at precedes created_at")
        if self.last_seen_at < self.created_at:
            raise ValueError("device last_seen_at precedes created_at")
        for instant in (
            self.push_token_updated_at,
            self.push_token_invalidated_at,
            self.revoked_at,
        ):
            if instant is not None and instant < self.created_at:
                raise ValueError("device lifecycle instant precedes creation")
        return self


def push_token_fingerprint(token: str) -> str:
    if not token:
        raise ValueError("push token fingerprint requires a token")
    return sha256(token.encode("utf-8")).hexdigest()[:6]
