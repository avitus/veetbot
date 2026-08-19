"""Provider-neutral rendered-browser observations."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.errors import AgentCoreError
from agent_core.domain.web import is_public_https_url


class BrowserProfileControlPlaneError(AgentCoreError):
    """Stable failure returned by the isolated profile lifecycle service."""

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


def browser_origin(value: str) -> str:
    """Return one normalized public HTTPS origin or raise ``ValueError``."""

    if not is_public_https_url(value):
        raise ValueError("browser origin must use public HTTPS")
    parsed = urlsplit(value)
    if parsed.hostname is None:
        raise ValueError("browser origin requires a hostname")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    return f"https://{hostname}"


def normalize_browser_origin(value: str) -> str:
    """Validate an operator-configured origin without path, query, or fragment."""

    parsed = urlsplit(value)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("browser origins cannot contain a path, query, or fragment")
    return browser_origin(value)


class BrowserActionKind(StrEnum):
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    CHECK = "check"
    PRESS = "press"
    SCROLL = "scroll"


class BrowserActionConsequence(StrEnum):
    ROUTINE = "routine"
    PAYMENT = "payment"
    PURCHASE = "purchase"
    ACCOUNT_RECOVERY = "account_recovery"
    AUTHENTICATION_CHANGE = "authentication_change"
    PERMISSION_CHANGE = "permission_change"
    LEGAL_ACCEPTANCE = "legal_acceptance"
    PUBLICATION = "publication"
    DESTRUCTIVE = "destructive"
    FILE_TRANSFER = "file_transfer"
    SECURITY_CHANGE = "security_change"
    UNKNOWN = "unknown"


class BrowserKey(StrEnum):
    ENTER = "Enter"
    ESCAPE = "Escape"
    TAB = "Tab"
    SPACE = "Space"
    ARROW_UP = "ArrowUp"
    ARROW_DOWN = "ArrowDown"
    ARROW_LEFT = "ArrowLeft"
    ARROW_RIGHT = "ArrowRight"


class BrowserInteractiveEvent(BaseModel):
    """One bounded event from the direct user authentication surface."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern="^(click|text|key)$")
    x: int | None = Field(default=None, ge=0, le=8192)
    y: int | None = Field(default=None, ge=0, le=8192)
    text: str | None = Field(default=None, max_length=4096, repr=False)
    key: str | None = Field(
        default=None,
        pattern="^(Enter|Escape|Tab|Backspace|ArrowUp|ArrowDown|ArrowLeft|ArrowRight)$",
    )

    @model_validator(mode="after")
    def fields_match_kind(self) -> BrowserInteractiveEvent:
        if self.kind == "click":
            valid = (
                self.x is not None and self.y is not None and self.text is None and self.key is None
            )
        elif self.kind == "text":
            valid = self.x is None and self.y is None and self.text is not None and self.key is None
        else:
            valid = self.x is None and self.y is None and self.text is None and self.key is not None
        if not valid:
            raise ValueError("browser interaction fields do not match its kind")
        return self


class BrowserProfileStatus(StrEnum):
    PROVISIONING = "provisioning"
    AUTHENTICATION_REQUIRED = "authentication_required"
    READY = "ready"
    NEEDS_USER = "needs_user"
    REVOKED = "revoked"


class BrowserAuthenticationStatus(StrEnum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    NEEDS_USER = "needs_user"
    READY = "ready"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


ALLOWED_BROWSER_AUTHENTICATION_TRANSITIONS: dict[
    BrowserAuthenticationStatus, frozenset[BrowserAuthenticationStatus]
] = {
    BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED: frozenset(
        {
            BrowserAuthenticationStatus.NEEDS_USER,
            BrowserAuthenticationStatus.READY,
            BrowserAuthenticationStatus.EXPIRED,
            BrowserAuthenticationStatus.CANCELLED,
        }
    ),
    BrowserAuthenticationStatus.NEEDS_USER: frozenset(
        {
            BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
            BrowserAuthenticationStatus.READY,
            BrowserAuthenticationStatus.EXPIRED,
            BrowserAuthenticationStatus.CANCELLED,
        }
    ),
    BrowserAuthenticationStatus.READY: frozenset(),
    BrowserAuthenticationStatus.EXPIRED: frozenset(),
    BrowserAuthenticationStatus.CANCELLED: frozenset(),
}


ALLOWED_BROWSER_PROFILE_TRANSITIONS: dict[BrowserProfileStatus, frozenset[BrowserProfileStatus]] = {
    BrowserProfileStatus.PROVISIONING: frozenset({BrowserProfileStatus.REVOKED}),
    BrowserProfileStatus.AUTHENTICATION_REQUIRED: frozenset(
        {
            BrowserProfileStatus.READY,
            BrowserProfileStatus.NEEDS_USER,
            BrowserProfileStatus.REVOKED,
        }
    ),
    BrowserProfileStatus.READY: frozenset(
        {
            BrowserProfileStatus.AUTHENTICATION_REQUIRED,
            BrowserProfileStatus.NEEDS_USER,
            BrowserProfileStatus.REVOKED,
        }
    ),
    BrowserProfileStatus.NEEDS_USER: frozenset(
        {
            BrowserProfileStatus.AUTHENTICATION_REQUIRED,
            BrowserProfileStatus.READY,
            BrowserProfileStatus.REVOKED,
        }
    ),
    BrowserProfileStatus.REVOKED: frozenset(),
}


class BrowserProfile(BaseModel):
    """Secret-free metadata for one principal-owned browser profile."""

    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    provider_name: str | None = Field(default=None, min_length=1, max_length=128)
    provider_ref: str | None = Field(default=None, min_length=1, max_length=512, repr=False)
    allowed_origins: tuple[str, ...] = Field(min_length=1, max_length=64)
    status: BrowserProfileStatus
    generation: int = Field(ge=0)
    encryption_key_version: str | None = Field(default=None, min_length=1, max_length=128)
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None

    @field_validator("allowed_origins")
    @classmethod
    def origins_are_exact_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_browser_origin(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("browser profile origins must be unique")
        return normalized

    @model_validator(mode="after")
    def timestamps_are_consistent(self) -> BrowserProfile:
        if self.updated_at < self.created_at:
            raise ValueError("browser profile update precedes creation")
        if self.last_used_at is not None and self.last_used_at < self.created_at:
            raise ValueError("browser profile use precedes creation")
        provider_values = (
            self.provider_name,
            self.provider_ref,
            self.encryption_key_version,
        )
        if self.status is BrowserProfileStatus.PROVISIONING:
            if any(value is not None for value in provider_values):
                raise ValueError("provisioning profile cannot have a provider binding")
        elif self.status is BrowserProfileStatus.REVOKED and all(
            value is None for value in provider_values
        ):
            pass
        elif any(value is None for value in provider_values):
            raise ValueError("non-provisioning profile requires a provider binding")
        return self


class BrowserProfileProvisioning(BaseModel):
    """Opaque result returned after a provider provisions encrypted material."""

    provider_name: str = Field(min_length=1, max_length=128)
    provider_ref: str = Field(min_length=1, max_length=512, repr=False)
    encryption_key_version: str = Field(min_length=1, max_length=128)


class BrowserProfileView(BaseModel):
    """Public profile metadata with no provider reference or secret material."""

    id: UUID
    allowed_origins: tuple[str, ...]
    status: BrowserProfileStatus
    generation: int
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None = None


class BrowserLease(BaseModel):
    """Secret orchestration-side handle for one isolated service lease."""

    lease_ref: str = Field(min_length=32, max_length=128, repr=False)
    expires_at: datetime


class BrowserAuthenticationView(BaseModel):
    """Secret-free state of one direct user authentication ceremony."""

    id: UUID
    profile_id: UUID
    status: BrowserAuthenticationStatus
    expires_at: datetime
    launch_url: str | None = Field(default=None, repr=False, max_length=4096)


class BrowserAuthenticationRecord(BaseModel):
    """Durable, secret-free state for a direct authentication ceremony."""

    id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    profile_id: UUID
    status: BrowserAuthenticationStatus
    expires_at: datetime
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def authentication_window_is_consistent(self) -> BrowserAuthenticationRecord:
        if self.expires_at <= self.created_at:
            raise ValueError("browser authentication expiry must follow creation")
        if self.expires_at - self.created_at > timedelta(minutes=5):
            raise ValueError("browser authentication cannot exceed five minutes")
        if self.updated_at < self.created_at:
            raise ValueError("browser authentication update precedes creation")
        return self


class BrowserAction(BaseModel):
    """One revision-bound interaction with no profile or credential selector."""

    kind: BrowserActionKind
    expected_revision: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=128)
    value: str | None = Field(default=None, max_length=4096)
    key: BrowserKey | None = None
    delta_y: int | None = Field(default=None, ge=-2000, le=2000)

    @model_validator(mode="after")
    def fields_match_action_kind(self) -> BrowserAction:
        if self.kind in {BrowserActionKind.TYPE, BrowserActionKind.SELECT}:
            valid = self.value is not None and self.key is None and self.delta_y is None
        elif self.kind is BrowserActionKind.PRESS:
            valid = self.value is None and self.key is not None and self.delta_y is None
        elif self.kind is BrowserActionKind.SCROLL:
            valid = (
                self.value is None
                and self.key is None
                and self.delta_y is not None
                and self.delta_y != 0
            )
        else:
            valid = self.value is None and self.key is None and self.delta_y is None
        if not valid:
            raise ValueError("browser action fields do not match its kind")
        return self


class BrowserActionContext(BaseModel):
    """Provider-authored action metadata that page content cannot override."""

    origin: str
    role: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=1024)
    consequence: BrowserActionConsequence
    revision: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=128)

    @field_validator("origin")
    @classmethod
    def origin_is_exact(cls, value: str) -> str:
        return normalize_browser_origin(value)


class BrowserGrant(BaseModel):
    """Exact, expiring authority created through a trusted user surface."""

    id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    profile_id: UUID
    profile_generation: int = Field(ge=0)
    agent_version: str = Field(min_length=1, max_length=255)
    policy_version: str = Field(min_length=1, max_length=255)
    allowed_origins: tuple[str, ...] = Field(min_length=1, max_length=64)
    action_kinds: tuple[BrowserActionKind, ...] = Field(min_length=1, max_length=6)
    element_roles: tuple[str, ...] = Field(default=(), max_length=64)
    element_names: tuple[str, ...] = Field(default=(), max_length=64)
    purpose: str | None = Field(default=None, min_length=1, max_length=255)
    starts_at: datetime
    expires_at: datetime
    approved_by: str = Field(min_length=1, max_length=255)
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("allowed_origins")
    @classmethod
    def grant_origins_are_exact_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_browser_origin(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("browser grant origins must be unique")
        return normalized

    @field_validator("action_kinds", "element_roles", "element_names")
    @classmethod
    def grant_constraints_are_unique(cls, values: tuple[object, ...]) -> tuple[object, ...]:
        if len(set(values)) != len(values):
            raise ValueError("browser grant constraints must be unique")
        return values

    @model_validator(mode="after")
    def grant_window_is_bounded(self) -> BrowserGrant:
        if self.expires_at <= self.starts_at:
            raise ValueError("browser grant expiry must follow its start")
        if self.expires_at - self.starts_at > timedelta(days=30):
            raise ValueError("browser grant cannot exceed thirty days")
        if self.updated_at < self.created_at:
            raise ValueError("browser grant update precedes creation")
        if self.revoked_at is not None and self.revoked_at < self.created_at:
            raise ValueError("browser grant revocation precedes creation")
        return self


class BrowserGrantAuthorization(BaseModel):
    allowed: bool
    reason_code: str = Field(min_length=1, max_length=128)


class BrowserGrantView(BaseModel):
    """Public, secret-free view of exact standing browser authority."""

    id: UUID
    profile_id: UUID
    profile_generation: int
    agent_version: str
    policy_version: str
    allowed_origins: tuple[str, ...]
    action_kinds: tuple[BrowserActionKind, ...]
    element_roles: tuple[str, ...]
    element_names: tuple[str, ...]
    purpose: str | None
    starts_at: datetime
    expires_at: datetime
    approved_by: str
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BrowserElement(BaseModel):
    """A bounded, opaque accessibility-tree element exposed to the model."""

    ref: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=1024)
    disabled: bool = False
    checked: bool | None = None


class BrowserObservation(BaseModel):
    """The safe subset of one rendered page revision."""

    url: str = Field(max_length=4096)
    title: str | None = Field(default=None, max_length=1024)
    revision: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=262_144)
    elements: tuple[BrowserElement, ...] = Field(default=(), max_length=256)

    @field_validator("url")
    @classmethod
    def url_is_public_https(cls, value: str) -> str:
        if not is_public_https_url(value):
            raise ValueError("browser observation URL is not public HTTPS")
        return value


class BrowserProviderError(RuntimeError):
    def __init__(self, reason_code: str, *, retryable: bool) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable
