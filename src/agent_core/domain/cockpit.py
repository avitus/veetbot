"""Cockpit widget, feed, layout and event value types (Milestone 35).

A widget is a package of generic code the assistant writes and the public
`cockpit/widgets` branch records; its revisions move from validation through
commit to live. Personal values never live in a package: they are the
widget's private settings and the feeds it declares. Every row is scoped to
one tenant and one principal. See docs/plan/cockpit.md.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

WIDGET_SLUG_PATTERN = r"^[a-z][a-z0-9-]{1,47}$"
FEED_ID_PATTERN = r"^(veetbot|agent)\.[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)*$"
FEED_ID_MAX_LENGTH = 64
FEED_SCHEMA_MAX_BYTES = 8 * 1024
FEED_SNAPSHOT_MAX_BYTES = 64 * 1024
FEED_ERROR_MAX_CHARS = 200
# The deepest history a manifest may declare, and so the most any feed stores.
FEED_HISTORY_MAX = 288
WIDGET_FILE_MAX_BYTES = 64 * 1024
WIDGET_PACKAGE_MAX_BYTES = 256 * 1024
WIDGET_PACKAGE_MAX_FILES = 12
WIDGET_BUNDLE_MAX_BYTES = 256 * 1024
WIDGET_SETTINGS_MAX_KEYS = 16
WIDGET_SETTINGS_MAX_CHARS = 200
LAYOUT_MAX_PLACEMENTS = 64
EVENT_SUBJECT_MAX_CHARS = 64
WIDGET_FILE_PATTERN = r"^(widget\.json|widget\.tsx|fixtures/[a-z0-9][a-z0-9-]{0,31}\.json)$"
_WIDGET_FILE = re.compile(WIDGET_FILE_PATTERN)
_SETTING_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_TRAILER = re.compile(r"^(Cockpit-Widget|Veetbot-Revision): (\S+)$")

type CommitOperation = Literal["create", "update", "delete", "restore", "revert"]
# A feed's JSON Schema document, as the assistant declared it.
type FeedSchemaDocument = dict[str, JsonValue]
# A feed snapshot's JSON value, checked against its feed's schema.
type FeedData = JsonValue
_COMMIT_OPERATIONS = frozenset({"create", "update", "delete", "restore", "revert"})


class WidgetState(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"


class RevisionState(StrEnum):
    VALIDATING = "validating"
    REJECTED = "rejected"
    PENDING_COMMIT = "pending_commit"
    COMMITTED = "committed"
    LIVE = "live"
    SUPERSEDED = "superseded"
    CONFLICTED = "conflicted"


# A revision carries its bundle from the build onwards, and its commit from the commit onwards.
_BUILT_STATES = frozenset(
    {
        RevisionState.PENDING_COMMIT,
        RevisionState.COMMITTED,
        RevisionState.LIVE,
        RevisionState.SUPERSEDED,
        RevisionState.CONFLICTED,
    }
)
_COMMITTED_STATES = frozenset(
    {RevisionState.COMMITTED, RevisionState.LIVE, RevisionState.SUPERSEDED}
)


class RevisionSource(StrEnum):
    ASSISTANT = "assistant"
    OWNER_GIT = "owner_git"
    REVERT = "revert"


class TileSize(StrEnum):
    SM = "sm"
    MD = "md"
    TALL = "tall"
    LG = "lg"
    WIDE = "wide"


class DeviceClass(StrEnum):
    COMPACT = "compact"
    REGULAR = "regular"
    WIDE = "wide"


class FeedTrust(StrEnum):
    PLATFORM = "platform"
    UNTRUSTED = "untrusted"


class CockpitEventKind(StrEnum):
    FEED_UPDATED = "feed.updated"
    WIDGET_REVISED = "widget.revised"
    WIDGET_DELETED = "widget.deleted"
    LAYOUT_UPDATED = "layout.updated"
    SETTINGS_UPDATED = "settings.updated"
    IMPORT_FAILED = "import.failed"


class OutboxOperation(StrEnum):
    WRITE = "write"
    REMOVE = "remove"


def package_violation(files: Mapping[str, str]) -> str | None:
    """Why a file map cannot be stored as a package, or None when it can."""

    if len(files) > WIDGET_PACKAGE_MAX_FILES:
        return f"a package holds at most {WIDGET_PACKAGE_MAX_FILES} files"
    total = 0
    for name, content in files.items():
        if not _WIDGET_FILE.fullmatch(name):
            return f"{name!r} is not a widget package file"
        size = len(content.encode("utf-8"))
        if size > WIDGET_FILE_MAX_BYTES:
            return f"{name} exceeds {WIDGET_FILE_MAX_BYTES} bytes"
        total += size
    if total > WIDGET_PACKAGE_MAX_BYTES:
        return f"a package exceeds {WIDGET_PACKAGE_MAX_BYTES} bytes"
    return None


def _json_bytes(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


class _Owned(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)


class BuildDiagnostic(BaseModel):
    """One builder finding. It names the rule and the file, never matched personal text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str = Field(min_length=1, max_length=64)
    file: str = Field(default="", max_length=64)
    message: str = Field(max_length=2000)
    line: int | None = Field(default=None, ge=1)
    size: str | None = Field(default=None, max_length=8)


class Widget(_Owned):
    slug: str = Field(pattern=WIDGET_SLUG_PATTERN)
    state: WidgetState
    live_revision_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class WidgetRevision(_Owned):
    """An immutable package and where it is on its way to the board."""

    id: UUID
    slug: str = Field(pattern=WIDGET_SLUG_PATTERN)
    source: RevisionSource
    state: RevisionState
    files: dict[str, str]
    diagnostics: tuple[BuildDiagnostic, ...] = ()
    bundle: str | None = Field(default=None, max_length=WIDGET_BUNDLE_MAX_BYTES)
    bundle_sha256: str | None = None
    kit_api: int | None = Field(default=None, ge=1)
    commit_sha: str | None = None
    origin_run_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("files")
    @classmethod
    def files_form_a_package(cls, files: dict[str, str]) -> dict[str, str]:
        violation = package_violation(files)
        if violation is not None:
            raise ValueError(violation)
        return files

    @model_validator(mode="after")
    def state_fixes_what_it_carries(self) -> WidgetRevision:
        built = self.state in _BUILT_STATES
        if built != (self.bundle is not None):
            raise ValueError("a revision carries its bundle exactly from the build onwards")
        if self.bundle is not None:
            digest = hashlib.sha256(self.bundle.encode("utf-8")).hexdigest()
            if self.bundle_sha256 != digest or self.kit_api is None:
                raise ValueError("a built revision records its bundle digest and kit_api")
        elif self.bundle_sha256 is not None:
            raise ValueError("an unbuilt revision has no bundle digest")
        if (self.state in _COMMITTED_STATES) != (self.commit_sha is not None):
            raise ValueError("a revision records its commit exactly once it is committed")
        if self.commit_sha is not None and not _COMMIT_SHA.fullmatch(self.commit_sha):
            raise ValueError("a commit is a 40-character hexadecimal sha")
        if (self.state is RevisionState.REJECTED) != bool(self.diagnostics):
            raise ValueError("a rejected revision, and only a rejected one, carries diagnostics")
        return self


class WidgetSettings(_Owned):
    """The owner's private values for one widget; never committed anywhere."""

    slug: str = Field(pattern=WIDGET_SLUG_PATTERN)
    values: dict[str, str | int | float | bool]
    updated_at: datetime

    @field_validator("values")
    @classmethod
    def values_are_bounded(
        cls, values: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        if len(values) > WIDGET_SETTINGS_MAX_KEYS:
            raise ValueError(f"settings hold at most {WIDGET_SETTINGS_MAX_KEYS} values")
        for key, value in values.items():
            if not _SETTING_KEY.fullmatch(key):
                raise ValueError(f"{key!r} is not a settings key")
            if isinstance(value, str) and len(value) > WIDGET_SETTINGS_MAX_CHARS:
                raise ValueError(f"a settings value holds at most {WIDGET_SETTINGS_MAX_CHARS}")
        return values


class Placement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(pattern=WIDGET_SLUG_PATTERN)
    size: TileSize


class Layout(_Owned):
    """One device class's board: an ordered flow of placements, versioned for compare-and-set."""

    device_class: DeviceClass
    placements: tuple[Placement, ...] = Field(max_length=LAYOUT_MAX_PLACEMENTS)
    revision: int = Field(ge=1)
    updated_at: datetime

    @field_validator("placements")
    @classmethod
    def each_widget_once(cls, placements: tuple[Placement, ...]) -> tuple[Placement, ...]:
        slugs = [placement.slug for placement in placements]
        if len(set(slugs)) != len(slugs):
            raise ValueError("a layout places each widget once")
        return placements


def feed_trust(feed_id: str) -> FeedTrust:
    """Platform projections are trusted; anything the assistant publishes is not."""

    return FeedTrust.PLATFORM if feed_id.startswith("veetbot.") else FeedTrust.UNTRUSTED


class Feed(_Owned):
    id: str = Field(pattern=FEED_ID_PATTERN, max_length=FEED_ID_MAX_LENGTH)
    schema_document: dict[str, JsonValue] = Field(alias="schema")
    schema_version: int = Field(default=1, ge=1)
    trust: FeedTrust | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    @field_validator("schema_document")
    @classmethod
    def schema_is_bounded(cls, schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if _json_bytes(schema) > FEED_SCHEMA_MAX_BYTES:
            raise ValueError(f"a feed schema holds at most {FEED_SCHEMA_MAX_BYTES} bytes")
        return schema

    @model_validator(mode="before")
    @classmethod
    def derive_trust(cls, values: object) -> object:
        if isinstance(values, dict) and values.get("trust") is None and "id" in values:
            return {**values, "trust": feed_trust(str(values["id"]))}
        return values

    @model_validator(mode="after")
    def trust_follows_namespace(self) -> Feed:
        if self.trust is not feed_trust(self.id):
            raise ValueError("a feed's trust follows its namespace")
        return self


class FeedSnapshot(_Owned):
    feed_id: str = Field(pattern=FEED_ID_PATTERN, max_length=FEED_ID_MAX_LENGTH)
    sequence: int = Field(ge=1)
    observed_at: datetime
    expected_interval_seconds: int = Field(ge=1)
    error: str | None = Field(default=None, max_length=FEED_ERROR_MAX_CHARS)
    data: JsonValue = None
    schema_version: int = Field(default=1, ge=1)
    recorded_at: datetime

    @field_validator("data")
    @classmethod
    def data_is_bounded(cls, data: JsonValue) -> JsonValue:
        if _json_bytes(data) > FEED_SNAPSHOT_MAX_BYTES:
            raise ValueError(f"a feed snapshot holds at most {FEED_SNAPSHOT_MAX_BYTES} bytes")
        return data


class CockpitEvent(_Owned):
    """One stream event. It names what changed; readers fetch the current state."""

    sequence: int = Field(ge=1)
    kind: CockpitEventKind
    subject: str = Field(min_length=1, max_length=EVENT_SUBJECT_MAX_CHARS)
    occurred_at: datetime


class CommitOutboxEntry(_Owned):
    revision_id: UUID
    slug: str = Field(pattern=WIDGET_SLUG_PATTERN)
    operation: OutboxOperation
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: datetime
    last_error: str | None = Field(default=None, max_length=500)
    expected_parent_sha: str | None = None
    created_at: datetime


class ImportFailure(_Owned):
    id: UUID
    commit_sha: str
    slug: str | None = Field(default=None, pattern=WIDGET_SLUG_PATTERN)
    diagnostics: tuple[BuildDiagnostic, ...] = Field(min_length=1)
    seen: bool = False
    created_at: datetime

    @field_validator("commit_sha")
    @classmethod
    def commit_is_a_sha(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("a commit is a 40-character hexadecimal sha")
        return value


WIDGET_BRANCH_DIRECTORY = "cockpit/widgets"
WIDGET_BRANCH_README = (
    "# Cockpit widgets\n\n"
    "Widget packages Veetbot's assistant and its owner write. Generic code only:\n"
    "personal values live in private settings and feeds, never here.\n"
)


def cockpit_channel(tenant_id: str) -> str:
    """One tenant's Cockpit LISTEN/NOTIFY channel, within PostgreSQL's 63-byte identifiers."""

    return f"cockpit_{hashlib.sha256(tenant_id.encode('utf-8')).hexdigest()[:32]}"


def is_bundle_digest(value: str) -> bool:
    return bool(_SHA256.fullmatch(value))


def is_widget_slug(value: str) -> bool:
    return bool(re.fullmatch(WIDGET_SLUG_PATTERN, value))


def is_widget_file(name: str) -> bool:
    """A package-relative file a widget may contain: its manifest, component, or a fixture."""

    return bool(_WIDGET_FILE.fullmatch(name))


def widget_commit_message(operation: str, slug: str, revision_id: UUID) -> str:
    """The only commit message the platform writes: no free text reaches the public branch."""

    if operation not in _COMMIT_OPERATIONS:
        raise ValueError(f"unknown widget commit operation {operation!r}")
    if not re.fullmatch(WIDGET_SLUG_PATTERN, slug):
        raise ValueError("a widget slug follows the manifest grammar")
    return (
        f"cockpit: {operation} {slug}\n\nCockpit-Widget: {slug}\nVeetbot-Revision: {revision_id}\n"
    )


def commit_trailers(message: str) -> dict[str, str]:
    """The Cockpit trailers of a commit message; a human commit usually has none."""

    trailers: dict[str, str] = {}
    for line in message.splitlines():
        match = _TRAILER.fullmatch(line.strip())
        if match:
            trailers[match.group(1)] = match.group(2)
    return trailers
