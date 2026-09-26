"""Time-boxed browser task grants created from an approval card (ADR-0129)."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.browser import (
    TASK_GRANT_PATH_SEGMENT,
    BrowserActionKind,
    browser_origin,
    normalize_browser_origin,
    require_task_grant_path_prefix,
)
from agent_core.domain.browser_classification import path_is_within_prefix, segment_is_sensitive

# Fixed limits, not configuration (ADR-0129 decision 4). Database check
# constraints enforce the same numbers.
TASK_GRANT_DURATION = timedelta(minutes=30)
TASK_GRANT_MAX_ACTIONS = 200
TASK_GRANT_MAX_TEXT_CHARACTERS = 256
TASK_GRANT_MAX_TYPED_CHARACTERS = 4096
TASK_GRANT_ACTION_KINDS: tuple[BrowserActionKind, ...] = tuple(BrowserActionKind)
MAXIMUM_TASK_GRANT_SCOPES = 16
PATH_SEGMENT = TASK_GRANT_PATH_SEGMENT

# Why an active task grant did not cover an approval's action: the closed list
# of the server-client contract. A client maps an unknown value to a generic
# sentence; the server never records one.
TASK_GRANT_NOT_COVERED_REASONS: frozenset[str] = frozenset(
    {
        f"browser.task_grant.{reason}"
        for reason in (
            "expired",
            "exhausted",
            "revoked",
            "ended",
            "unavailable",
            "policy_changed",
            "profile_changed",
            "agent_changed",
            "scope_removed",
            "run_not_eligible",
            "turn_not_browser_only",
            "outside_origin",
            "outside_prefix",
            "sensitive_path",
            "unnamed_element",
            "field_not_covered",
            "text_too_long",
            "text_not_covered",
            "text_budget_exhausted",
            "link_outside_prefix",
            "form_outside_prefix",
            "facts_unavailable",
            "excluded.payment",
            "excluded.purchase",
            "excluded.account_recovery",
            "excluded.authentication_change",
            "excluded.permission_change",
            "excluded.legal_acceptance",
            "excluded.publication",
            "excluded.destructive",
            "excluded.file_transfer",
            "excluded.security_change",
        )
    }
)


def task_grant_id_for_approval(approval_id: UUID) -> UUID:
    """The grant an ``approve_for_task`` resolution creates, so a retry creates none."""

    return uuid5(NAMESPACE_URL, f"veetbot:browser-task-grant:{approval_id}")


class BrowserTaskGrantScope(BaseModel):
    """One configured site scope: an exact origin and one path segment (ADR-0129).

    A grant copies its origin and prefix from the scope that contains the page,
    never from the page URL.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str
    path_prefix: str

    @field_validator("origin")
    @classmethod
    def origin_is_exact(cls, value: str) -> str:
        return normalize_browser_origin(value)

    @field_validator("path_prefix")
    @classmethod
    def prefix_is_one_plain_segment(cls, value: str) -> str:
        require_task_grant_path_prefix(value)
        if segment_is_sensitive(value[1:]):
            raise ValueError("a task-grant scope cannot name a sensitive path segment")
        return value

    def contains(self, url: str) -> bool:
        try:
            origin = browser_origin(url)
        except ValueError:
            return False
        return origin == self.origin and path_is_within_prefix(urlsplit(url).path, self.path_prefix)


def _parse_scope(entry: str) -> BrowserTaskGrantScope:
    if not entry:
        raise ValueError("is empty")
    if "?" in entry or "#" in entry:
        raise ValueError("has a query or a fragment")
    parsed = urlsplit(entry)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise ValueError("is not an HTTPS origin and path")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("carries credentials")
    try:
        origin = browser_origin(entry)
    except ValueError as exc:
        raise ValueError("is not a public HTTPS origin") from exc
    segments = parsed.path.split("/")
    if len(segments) != 2 or segments[0] != "" or not segments[1]:
        raise ValueError("does not have exactly one path segment")
    if TASK_GRANT_PATH_SEGMENT.fullmatch(segments[1]) is None:
        raise ValueError("has a path segment that is not a plain name")
    if segment_is_sensitive(segments[1]):
        raise ValueError("has a sensitive path segment")
    return BrowserTaskGrantScope(origin=origin, path_prefix=parsed.path)


def parse_task_grant_scopes(raw: str) -> tuple[BrowserTaskGrantScope, ...]:
    """Parse ``BROWSER_TASK_GRANT_SCOPES``: comma-separated origin-and-segment scopes.

    Empty or blank gives none. ``ValueError`` names the 1-based entry for a
    non-HTTPS or non-public origin; credentials, a port other than the HTTPS
    default, a query or a fragment; a path that is not exactly one segment,
    with no trailing slash; a segment that is not a plain name; a duplicate;
    and any entry past ``MAXIMUM_TASK_GRANT_SCOPES``.
    """

    if not raw.strip():
        return ()
    scopes: list[BrowserTaskGrantScope] = []
    for position, entry in enumerate(raw.split(","), start=1):
        if position > MAXIMUM_TASK_GRANT_SCOPES:
            raise ValueError(
                f"entry {position}: at most {MAXIMUM_TASK_GRANT_SCOPES} scopes are allowed"
            )
        try:
            scope = _parse_scope(entry.strip())
        except ValueError as exc:
            raise ValueError(f"entry {position} {exc}") from None
        if scope in scopes:
            raise ValueError(f"entry {position} repeats an earlier scope")
        scopes.append(scope)
    return tuple(scopes)


def offer_scope(
    page_url: str, scopes: tuple[BrowserTaskGrantScope, ...]
) -> BrowserTaskGrantScope | None:
    """The one configured scope that contains ``page_url``, if any.

    Configuration keeps entries unique and one segment deep, so at most one
    scope contains a page.
    """

    return next((scope for scope in scopes if scope.contains(page_url)), None)


class BrowserTaskGrantEndReason(StrEnum):
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"
    REVOKED = "revoked"
    SUPERSEDED = "superseded"
    PROFILE_CHANGED = "profile_changed"
    PROFILE_REVOKED = "profile_revoked"
    POLICY_CHANGED = "policy_changed"
    AGENT_CHANGED = "agent_changed"
    SCOPE_REMOVED = "scope_removed"


class BrowserTaskGrantOffer(BaseModel):
    """The server-authored offer stored on a ``browser.act`` approval.

    Its origin and prefix are the configured scope's; the limits are fixed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str
    path_prefix: str
    duration_seconds: Literal[1800] = 1800
    max_actions: Literal[200] = 200
    max_typed_characters: Literal[4096] = 4096
    action_kinds: tuple[BrowserActionKind, ...] = TASK_GRANT_ACTION_KINDS
    summary: str = Field(min_length=1, max_length=1024)

    @field_validator("origin")
    @classmethod
    def origin_is_exact(cls, value: str) -> str:
        return normalize_browser_origin(value)

    @field_validator("path_prefix")
    @classmethod
    def prefix_is_one_segment(cls, value: str) -> str:
        return require_task_grant_path_prefix(value)


class TaskGrantEcho(BaseModel):
    """The offer's origin and prefix, repeated in an ``approve_for_task`` body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: str = Field(min_length=1, max_length=512)
    path_prefix: str = Field(min_length=2, max_length=65)


class TaskGrantNotCovered(BaseModel):
    """Why the session's active task grant did not cover an approval's action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: UUID
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_is_closed(cls, value: str) -> str:
        if value not in TASK_GRANT_NOT_COVERED_REASONS:
            raise ValueError("task grant reason is not in the closed list")
        return value


class BrowserTaskGrant(BaseModel):
    """Session-bound, time-boxed authority for ``browser.act`` inside one scope."""

    id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    session_id: UUID
    profile_id: UUID
    profile_generation: int = Field(ge=0)
    agent_version: str = Field(min_length=1, max_length=255)
    policy_version: str = Field(min_length=1, max_length=255)
    origin: str
    path_prefix: str
    max_actions: int = Field(ge=1, le=TASK_GRANT_MAX_ACTIONS)
    actions_used: int = Field(ge=0)
    typed_characters: int = Field(ge=0, le=TASK_GRANT_MAX_TYPED_CHARACTERS)
    approval_id: UUID
    approved_by: str = Field(min_length=1, max_length=255)
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: BrowserTaskGrantEndReason | None = None

    @field_validator("origin")
    @classmethod
    def origin_is_exact(cls, value: str) -> str:
        return normalize_browser_origin(value)

    @field_validator("path_prefix")
    @classmethod
    def prefix_is_one_segment(cls, value: str) -> str:
        return require_task_grant_path_prefix(value)

    @model_validator(mode="after")
    def window_caps_and_end_are_consistent(self) -> BrowserTaskGrant:
        if self.expires_at <= self.created_at:
            raise ValueError("task grant expiry must follow its creation")
        if self.expires_at - self.created_at > TASK_GRANT_DURATION:
            raise ValueError("task grant cannot exceed thirty minutes")
        if self.actions_used > self.max_actions:
            raise ValueError("task grant uses cannot exceed its action cap")
        if (self.ended_at is None) != (self.end_reason is None):
            raise ValueError("task grant end time and reason are set together")
        if self.revoked_at is not None and self.end_reason is not BrowserTaskGrantEndReason.REVOKED:
            raise ValueError("a revoked task grant ends with reason revoked")
        return self


class BrowserTaskGrantStatus(StrEnum):
    """Derived for views, never stored."""

    ACTIVE = "active"
    EXPIRED = "expired"
    EXHAUSTED = "exhausted"
    REVOKED = "revoked"
    ENDED = "ended"


class BrowserTaskGrantView(BaseModel):
    """The public view of one task grant."""

    model_config = ConfigDict(extra="forbid")
