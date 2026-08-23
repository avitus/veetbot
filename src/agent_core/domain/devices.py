"""Device identity domain values."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

from agent_core.domain.notifications import NotificationKind


def _aware_utc(value: datetime, subject: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{subject} must be aware")
    return value.astimezone(UTC)


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


@dataclass(frozen=True)
class DeviceRoutingIssue:
    """One shared push-routing rule violation with an API-stable reason code."""

    reason_code: str
    message: str


def device_routing_issue(
    *,
    kind: DeviceKind,
    provider: PushProvider | None,
    token_present: bool,
    environment: PushEnvironment | None,
    app_bundle_id_present: bool,
) -> DeviceRoutingIssue | None:
    """Validate routing consistently for API input and durable device rows."""

    if (provider is None) != (not token_present):
        return DeviceRoutingIssue(
            "device.push_routing_incomplete",
            "push provider and token must be present together",
        )
    if provider is PushProvider.APNS:
        if environment is None or not app_bundle_id_present:
            return DeviceRoutingIssue(
                "device.apns_configuration_incomplete",
                "APNs registration requires environment and bundle identifier",
            )
    elif environment is not None:
        return DeviceRoutingIssue(
            "device.push_environment_without_apns",
            "only APNs registration accepts a push environment",
        )
    if provider is PushProvider.TELEGRAM and kind is not DeviceKind.SURFACE:
        return DeviceRoutingIssue(
            "device.telegram_kind_invalid",
            "Telegram registration requires a surface device",
        )
    if (
        kind is DeviceKind.SURFACE
        and provider is not None
        and provider is not PushProvider.TELEGRAM
    ):
        return DeviceRoutingIssue(
            "device.surface_routing_incomplete",
            "surface registration accepts only Telegram routing",
        )
    return None


class DeviceRegistration(BaseModel):
    """Validated registration material before server-owned lifecycle fields exist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_device_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    kind: DeviceKind
    platform: str = Field(min_length=1, max_length=64)
    app_bundle_id: str | None = Field(default=None, min_length=1, max_length=255)
    push_provider: PushProvider | None = None
    push_token: SecretStr | None = None
    push_environment: PushEnvironment | None = None
    muted_kinds: frozenset[NotificationKind] = frozenset()


class DeviceRegistrationIdempotencyRecord(BaseModel):
    """Principal-scoped exact request and safe response snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=255)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: dict[str, JsonValue]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value, "device idempotency created_at")


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
        return _aware_utc(value, "device instants")

    @field_validator("push_token")
    @classmethod
    def push_token_is_not_blank(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            raise ValueError("push token cannot be empty")
        return value

    @model_validator(mode="after")
    def push_and_status_are_consistent(self) -> Device:
        issue = device_routing_issue(
            kind=self.kind,
            provider=self.push_provider,
            token_present=self.push_token is not None,
            environment=self.push_environment,
            app_bundle_id_present=self.app_bundle_id is not None,
        )
        if issue is not None:
            raise ValueError(issue.message)

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


class DeviceCursor(BaseModel):
    """Stable descending device-list cursor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created_at: datetime
    id: UUID

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value, "device cursor created_at")


class PushTarget(BaseModel):
    """Secret-bearing routing value passed only to a push transport."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: UUID
    provider: PushProvider
    token: SecretStr
    environment: PushEnvironment | None = None
    app_bundle_id: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("token")
    @classmethod
    def token_is_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("push target token cannot be empty")
        return value

    @model_validator(mode="after")
    def provider_fields_are_consistent(self) -> PushTarget:
        if self.provider is PushProvider.APNS:
            if self.environment is None or self.app_bundle_id is None:
                raise ValueError("APNs push target requires environment and bundle identifier")
        elif self.environment is not None:
            raise ValueError("only APNs push target may carry an environment")
        return self


def push_token_fingerprint(token: str) -> str:
    if not token:
        raise ValueError("push token fingerprint requires a token")
    return sha256(token.encode("utf-8")).hexdigest()[:6]
