"""Provider-neutral rendered-browser observations."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.browser_evidence import BrowserEvidence, BrowserSimpleEvidence
from agent_core.domain.browser_extraction import BrowserExtractionResult
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


def require_service_origin(value: str, *, message: str) -> str:
    """Return one HTTPS service origin with no credentials or URL components."""

    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(message) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError(message)
    return value.rstrip("/")


# Fixed trusted-tool metadata, never a page-authored instruction or effect proof.
BROWSER_AUTH_INTERRUPTION_MARKER = '{"browser_interruption":"needs_user"}'


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


class BrowserAuthenticationMode(StrEnum):
    """How an authentication ceremony signs the user in (ADR-0128).

    ``remote`` drives the isolated service's headed browser; ``device`` has the
    user's own client hand one site's session to the service, which verifies
    it before sealing.
    """

    REMOTE = "remote"
    DEVICE = "device"


class BrowserPageEvidence(BaseModel):
    """What one verification load of a confirmed page found (ADR-0128).

    The path can carry tokens, so it never appears in a representation.
    """

    model_config = ConfigDict(frozen=True)

    on_allowed_origin: bool
    path: str = Field(max_length=4096, repr=False)
    challenge_visible: bool


class BrowserVerificationStage(StrEnum):
    """What one verification load is doing; all a diagnostic may say of it (ADR-0128)."""

    START = "start"
    NAVIGATE = "navigate"
    IDLE = "idle"
    INSPECT = "inspect"
    SETTLE = "settle"
    REINSPECT = "reinspect"
    CAPTURE = "capture"


def ignore_verification_stage(stage: BrowserVerificationStage) -> None:
    """The stage observer of a caller that keeps no verification diagnostic."""
    del stage


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


# The hosted service caps each lease request at fifteen minutes and a lease's
# whole life, renewals included, at sixty (ADR-0127).
MAXIMUM_BROWSER_LEASE_SECONDS = 15 * 60
MAXIMUM_BROWSER_LEASE_LIFETIME_SECONDS = 60 * 60


class BrowserRunState(StrEnum):
    """What a hosted lease's run needs from it."""

    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    RESUMING = "resuming"
    ENDED = "ended"


class BrowserLease(BaseModel):
    """Secret orchestration-side handle for one isolated service lease."""

    lease_ref: str = Field(min_length=32, max_length=128, repr=False)
    expires_at: datetime
    # The last action sequence the service applied on this lease; a caller
    # that reattaches continues from it.
    sequence: int = Field(default=0, ge=0)


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


class BrowserAuthenticationWait(BaseModel):
    """Trusted run checkpoint binding; never accepted from a model or page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: UUID
    generation: int = Field(ge=1)
    started_at: AwareDatetime
    expires_at: AwareDatetime
    question_id: UUID | None = None
    resume_url: str | None = Field(default=None, max_length=4096, repr=False)


TERMINAL_BROWSER_AUTHENTICATION_STATUSES = frozenset(
    {
        BrowserAuthenticationStatus.READY,
        BrowserAuthenticationStatus.EXPIRED,
        BrowserAuthenticationStatus.CANCELLED,
    }
)


def sign_in_outcome_unrecorded(records: Sequence[BrowserAuthenticationRecord]) -> bool:
    """Whether a profile's newest sign-in has no recorded outcome (ADR-0128 D10).

    A begin advances the generation that grants pin, and only a ``ready``
    outcome, once recorded, advances it again. A grant created in between
    would pin the begin's generation and keep authorizing on the session the
    service seals if no client ever records that outcome. The service holds
    one open ceremony per profile, so only the newest record can still seal.
    Its expiry settles nothing: a handoff accepted before it seals after it,
    and a sealed outcome may never be read.
    """

    if not records:
        return False
    newest = max(record.created_at for record in records)
    return any(
        record.created_at == newest
        and record.status not in TERMINAL_BROWSER_AUTHENTICATION_STATUSES
        for record in records
    )


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


class BrowserObservationExpansion(BaseModel):
    """One continuation bound to a provider's current observation."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    after: str | None = Field(default=None, min_length=1, max_length=128)
    cursor: str | None = Field(default=None, min_length=32, max_length=128)

    region_ref: str | None = Field(default=None, min_length=1, max_length=128)
    expected_revision: str | None = Field(default=None, min_length=1, max_length=128)
    text_offset: int = Field(default=0, ge=0, le=262_144)

    @model_validator(mode="after")
    def exactly_one_continuation(self) -> BrowserObservationExpansion:
        if self.region_ref is not None:
            if self.expected_revision is None or not self.model_fields_set <= {
                "region_ref",
                "expected_revision",
                "text_offset",
            }:
                raise ValueError("region expansion requires its revision and optional text offset")
        elif len(self.model_fields_set) != 1 or (self.after is None) == (self.cursor is None):
            raise ValueError("exactly one browser continuation is required")
        return self


class BrowserObservationCoverage(BaseModel):
    """Bounds of one live candidate window, not a whole-page completeness claim."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    candidate_offset: int = Field(ge=0, le=65_536)
    scanned_candidates: int = Field(ge=0, le=4_096)
    next_cursor: str | None = Field(default=None, min_length=32, max_length=128)
    scan_limit_reached: bool = False


class BrowserCondition(BaseModel):
    """A positive predicate over visible controls, never authorization or effect proof."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    role: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=1024)
    disabled: bool | None = None
    checked: bool | None = None
    timeout_ms: int = Field(default=2000, ge=0, le=5000)
    evidence: BrowserEvidence | None = None
    failure_evidence: BrowserSimpleEvidence | None = None

    @model_validator(mode="after")
    def one_positive_predicate(self) -> BrowserCondition:
        if self.evidence is None:
            if self.role is None or self.name is None:
                raise ValueError("a positive control or evidence predicate is required")
        elif any(key in self.model_fields_set for key in ("role", "name", "disabled", "checked")):
            raise ValueError("control and evidence predicates cannot be mixed")
        return self


class BrowserConditionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["satisfied", "not_observed", "ambiguous", "failed"]
    scope: Literal["observation_window"] = "observation_window"
    observations: int = Field(ge=1, le=21)
    elapsed_ms: int = Field(ge=0, le=5000)


class BrowserSemanticRegion(BaseModel):
    """Visible page evidence, not an actionable element or continuation anchor."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    ref: str = Field(min_length=1, max_length=128)
    kind: Literal["dialog", "alert", "status", "form", "heading", "main", "section"]
    text: str = Field(max_length=512)
    text_truncated: bool = False


class BrowserRegionCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = 1
    scope: Literal["main_document"] = "main_document"
    scanned_nodes: int = Field(ge=0, le=8192)
    scan_limit_reached: bool = False
    omitted_regions: int = Field(default=0, ge=0, le=8192)


class BrowserTextCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = 1
    scope: Literal["main_document_and_open_shadow"] = "main_document_and_open_shadow"
    scanned_nodes: int = Field(ge=0, le=8192)
    scanned_text_characters: int = Field(ge=0, le=262_144)
    node_limit_reached: bool = False
    text_limit_reached: bool = False
    omitted_text_bytes: int = Field(default=0, ge=0, le=262_144)


class BrowserObservationFocus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    region_ref: str = Field(min_length=1, max_length=128)
    text_offset: int = Field(ge=0, le=262_144)
    text_total_bytes: int = Field(ge=0, le=262_144)
    text_cursor: str = Field(min_length=32, max_length=64)


class BrowserObservation(BaseModel):
    """The safe subset of one rendered page revision."""

    url: str = Field(max_length=4096)
    title: str | None = Field(default=None, max_length=1024)
    revision: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=262_144)
    text_coverage: BrowserTextCoverage | None = None
    focus: BrowserObservationFocus | None = None
    elements: tuple[BrowserElement, ...] = Field(default=(), max_length=256)
    coverage: BrowserObservationCoverage | None = None
    readiness: Literal["dom_quiet", "bound_expired"] | None = None
    interruption: Literal["needs_user"] | None = None
    condition: BrowserConditionResult | None = None
    regions: tuple[BrowserSemanticRegion, ...] = Field(default=(), max_length=32)
    region_coverage: BrowserRegionCoverage | None = None
    extraction: BrowserExtractionResult | None = None

    @model_validator(mode="after")
    def region_evidence_is_distinct(self) -> BrowserObservation:
        if self.text_coverage is not None and (
            len(self.text.encode("utf-8")) + self.text_coverage.omitted_text_bytes > 262_144
        ):
            raise ValueError("readable text capture exceeds its byte limit")
        if self.interruption is not None and (self.elements or self.regions or self.focus):
            raise ValueError("interrupted observations cannot carry actionable references")
        if self.focus is not None and (
            self.focus.text_offset + len(self.text.encode("utf-8")) > self.focus.text_total_bytes
        ):
            raise ValueError("focused text exceeds its captured range")
        if self.extraction is not None and self.extraction.revision != self.revision:
            raise ValueError("extraction must describe the observation revision")
        if self.regions and self.region_coverage is None:
            raise ValueError("semantic regions require coverage")
        references = {region.ref for region in self.regions}
        if len(references) != len(self.regions) or references.intersection(
            element.ref for element in self.elements
        ):
            raise ValueError("semantic references must be unique and distinct from controls")
        return self

    @field_validator("url")
    @classmethod
    def url_is_public_https(cls, value: str) -> str:
        if not is_public_https_url(value):
            raise ValueError("browser observation URL is not public HTTPS")
        return value


# One path segment of a task-grant scope (ADR-0129). A scope's prefix is "/"
# followed by exactly one such segment.
TASK_GRANT_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")
# Element facts carry each label source cut to this length (ADR-0129).
MAXIMUM_FACT_LABEL_CHARACTERS = 256


def require_task_grant_path_prefix(value: str) -> str:
    """Return ``value`` when it is "/" plus one path segment, or raise ``ValueError``."""

    if not value.startswith("/") or TASK_GRANT_PATH_SEGMENT.fullmatch(value[1:]) is None:
        raise ValueError("a task-grant path prefix is one path segment")
    return value


class BrowserFieldKind(StrEnum):
    """The closed kind of an element's entry control, derived by the runtime.

    The kinds follow what the runtime will act on (ADR-0129): it types only
    into an ``input`` or a ``textarea``, selects only in a ``select``, and
    checks only a native check box or radio, so a control the page built from
    another element has a kind of its own.
    """

    NONE = "none"
    TEXT = "text"
    SEARCH = "search"
    # A ``textarea``.
    MULTILINE = "multiline"
    # A region the page made editable (``contenteditable``), not an ``input``
    # or a ``textarea``: keys reach it, typed text does not.
    EDITABLE = "editable"
    # A native check box or radio (``input type=checkbox|radio``).
    CHOICE = "choice"
    # A ``select``.
    SELECT = "select"
    # Any other element with a check box, radio, option or menu radio role.
    CUSTOM_CHOICE = "custom_choice"
    EMAIL = "email"
    TELEPHONE = "telephone"
    URL = "url"
    NUMBER = "number"
    DATE = "date"
    IDENTITY = "identity"
    PAYMENT = "payment"
    PASSWORD = "password"
    ONE_TIME_CODE = "one_time_code"
    FILE = "file"
    OTHER = "other"


class BrowserLabelSource(StrEnum):
    """Each source of an element's label, classified separately (ADR-0129)."""

    ARIA_LABEL = "aria_label"
    ARIA_LABELLEDBY = "aria_labelledby"
    LABEL = "label"
    TITLE = "title"
    PLACEHOLDER = "placeholder"
    ALT = "alt"
    VALUE = "value"
    VISIBLE_TEXT = "visible_text"


class BrowserTargetFacts(BaseModel):
    """A link or form target reduced inside the runtime; never a raw URL.

    ``sensitive_path`` is true when any segment of the target's path is
    sensitive, and defaults to true so a missing value denies.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    same_origin: bool
    first_segment: str | None = Field(default=None, max_length=64)
    sensitive_path: bool = True


class BrowserElementFacts(BaseModel):
    """Secret-free facts about one observed element; never model-visible.

    ``labels`` holds the label sources the classifier reads differently from
    the element's name, each cut to 256 characters, so it is usually empty; a
    source left out reads exactly as the name does.
    ``labels_truncated`` is true when the facts and the name together do not
    carry every label source whole: one of them was cut, or was longer than
    the 1,024 characters the runtime reads. The runtime always sets it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_kind: BrowserFieldKind
    labels: dict[
        BrowserLabelSource, Annotated[str, Field(max_length=MAXIMUM_FACT_LABEL_CHARACTERS)]
    ] = Field(default_factory=dict, max_length=len(BrowserLabelSource))
    labels_truncated: bool = False
    link_target: BrowserTargetFacts | None = None
    form_target: BrowserTargetFacts | None = None
    download: bool = False
    context_name: str = Field(default="", max_length=128)


class BrowserObservationFacts(BaseModel):
    """Element facts for one observation revision, keyed by element reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: str = Field(min_length=1, max_length=128)
    elements: dict[Annotated[str, Field(min_length=1, max_length=128)], BrowserElementFacts] = (
        Field(default_factory=dict, max_length=256)
    )


class BrowserSnapshot(BaseModel):
    """What the hosted client returns: the observation and its optional facts.

    Facts are ``None`` from an older service or when they exceeded the
    service's budget; an element without facts is never covered by a grant.
    """

    observation: BrowserObservation
    facts: BrowserObservationFacts | None = None


class BrowserDispatchConstraint(BaseModel):
    """What a grant-authorized act may do; the runtime uses it only to refuse.

    The grant kind fixes the other fields: a task grant has one origin, a path
    prefix, an ``unknown`` ceiling and a 256-character text cap; a standing
    grant has a ``routine`` ceiling and neither prefix nor text cap. Specific
    Follow consent has the exact X origin, handle and profile path. Other field
    combinations are invalid and the isolated service answers with ``400``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_kind: Literal["task", "standing", "follow"]
    origins: tuple[str, ...] = Field(min_length=1, max_length=64)
    path_prefix: str | None = None
    not_after: AwareDatetime
    consequence_ceiling: Literal["routine", "unknown"]
    follow_handle: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,15}$")
    max_text_characters: Literal[256] | None

    @field_validator("origins")
    @classmethod
    def origins_are_exact_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_browser_origin(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("dispatch constraint origins must be unique")
        return normalized

    @field_validator("path_prefix")
    @classmethod
    def prefix_is_one_segment(cls, value: str | None) -> str | None:
        return None if value is None else require_task_grant_path_prefix(value)

    @model_validator(mode="after")
    def fields_match_grant_kind(self) -> BrowserDispatchConstraint:
        if self.grant_kind == "follow":
            valid = (
                self.origins == ("https://x.com",)
                and self.follow_handle is not None
                and self.path_prefix == f"/{self.follow_handle}"
                and self.consequence_ceiling == "unknown"
                and self.max_text_characters is None
            )
        elif self.grant_kind == "task":
            valid = (
                self.consequence_ceiling == "unknown"
                and self.max_text_characters == 256
                and self.path_prefix is not None
                and len(self.origins) == 1
            )
        else:
            valid = (
                self.consequence_ceiling == "routine"
                and self.max_text_characters is None
                and self.path_prefix is None
            )
        if not valid or (self.grant_kind != "follow" and self.follow_handle is not None):
            raise ValueError("dispatch constraint fields do not match its grant kind")
        return self


class BrowserCoverage(BaseModel):
    """Whether a grant covers one action, and the first rule that failed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    covered: bool
    consequence: BrowserActionConsequence
    reason: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def reason_names_a_refusal(self) -> BrowserCoverage:
        if self.covered == (self.reason is not None):
            raise ValueError("coverage names a reason exactly when it refuses")
        return self


class BrowserProviderError(RuntimeError):
    def __init__(self, reason_code: str, *, retryable: bool) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable
