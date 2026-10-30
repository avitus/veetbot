"""The bounded program and result of one agent-written job (ADR-0268)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_serializer, model_validator

from agent_core.domain.cockpit import FEED_ID_PATTERN, WIDGET_SLUG_PATTERN
from agent_core.domain.web import is_public_https_url

JOB_CALLS = frozenset({"http.request", "model.complete", "feed.publish", "log"})
JOB_SOURCE_BYTES = 64 * 1024
JOB_INPUT_BYTES = 64 * 1024
JOB_RESULT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class JobProgram:
    source: str
    calls: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        """Bound source size and reject unsupported broker operations."""
        if not self.source.strip() or len(self.source.encode("utf-8")) > JOB_SOURCE_BYTES:
            raise ValueError("Job source must contain between 1 byte and 64 KiB.")
        if not self.calls <= JOB_CALLS:
            raise ValueError("Job declares an unsupported broker operation.")


@dataclass(frozen=True, slots=True)
class JobResult:
    value: Any
    diagnostics: str
    broker_calls: int


class JobExecutionError(ValueError):
    """A job failed; the message is untrusted repair feedback for its author."""


@dataclass(frozen=True, slots=True)
class JobAccess:
    """Private access supplied only after the application's approval check."""

    inputs: dict[str, Any]
    hosts: dict[str, str | None]
    feeds: frozenset[str] = frozenset()
    timeout_seconds: int = 60
    maximum_requests: int = 50
    maximum_response_bytes: int = 48 * 1024
    model_cost: str = "0"

    def __post_init__(self) -> None:
        """Validate private inputs, destinations, feed authority, and execution limits."""
        try:
            cost = Decimal(self.model_cost)
        except InvalidOperation as exc:
            raise ValueError("Job model cost must be a decimal amount.") from exc
        if not cost.is_finite() or cost < 0:
            raise ValueError("Job model cost must be a finite non-negative amount.")
        for value, maximum in (
            (self.timeout_seconds, 600),
            (self.maximum_requests, 50),
            (self.maximum_response_bytes, 48 * 1024),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Job access limits are outside the supported bounds.")
        for host, credential in self.hosts.items():
            if not is_public_https_url(f"https://{host}") or host != host.lower().rstrip("."):
                raise ValueError("A job host must be an exact public DNS hostname.")
            if any(character in host for character in "/:@?#"):
                raise ValueError("A job host must not contain a URL path or port.")
            if credential is not None and (host, credential) != ("api.github.com", "github-reader"):
                raise ValueError("The job credential is not bound to that host.")
        if any(
            not feed.startswith("agent.")
            or len(feed) > 64
            or not re.fullmatch(FEED_ID_PATTERN, feed)
            for feed in self.feeds
        ):
            raise ValueError("Jobs publish only declared agent feeds.")
        if len(json.dumps(self.inputs, allow_nan=False).encode()) > JOB_INPUT_BYTES:
            raise ValueError("Job inputs exceed 64 KiB.")


class JobBrokerError(ValueError):
    """A content-free broker failure safe to return to sandbox code."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class JobManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    calls: frozenset[str] = frozenset()
    feeds: frozenset[str] = frozenset()

    @field_serializer("calls", "feeds")
    def sorted_declarations(self, values: frozenset[str]) -> list[str]:
        """Keep public manifest bytes stable across worker processes."""
        return sorted(values)

    @model_validator(mode="after")
    def operations_are_supported(self) -> JobManifest:
        """Reject unknown operations and feed declarations without feed publication."""
        if not self.calls <= JOB_CALLS:
            raise ValueError("Job declares an unsupported broker operation.")
        if self.feeds and "feed.publish" not in self.calls:
            raise ValueError("Feed declarations require feed.publish.")
        return self


class JobRevision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    manifest: JobManifest
    source: str
    state: str = Field(pattern="^(pending_commit|live|superseded|rejected|conflicted|archived)$")
    commit_sha: str | None = Field(default=None, pattern="^[a-f0-9]{40}$")
    created_at: datetime

    @model_validator(mode="after")
    def source_is_bounded(self) -> JobRevision:
        JobProgram(self.source, self.manifest.calls)
        return self

    def files(self) -> dict[str, str]:
        return {"job.json": self.manifest.model_dump_json(indent=2) + "\n", "main.py": self.source}


class JobGrant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    access: JobAccess
    created_at: datetime
    approval_id: UUID | None = None


class JobRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    principal_id: str
    slug: str = Field(pattern=WIDGET_SLUG_PATTERN)
    version: int = Field(ge=1)
    archived: bool = False
    live_revision_id: UUID | None = None
    revisions: dict[str, JobRevision] = Field(default_factory=dict)
    grants: dict[str, JobGrant] = Field(default_factory=dict)
    updated_at: datetime

    @model_validator(mode="after")
    def references_are_local(self) -> JobRecord:
        """Require revision and grant keys to match locally stored immutable identities."""
        if any(key != str(value.id) for key, value in self.revisions.items()):
            raise ValueError("Revision keys must match their ids.")
        if any(key != str(value.id) for key, value in self.grants.items()):
            raise ValueError("Grant keys must match their ids.")
        if self.live_revision_id is not None:
            revision = self.revisions.get(str(self.live_revision_id))
            if revision is None or revision.commit_sha is None:
                raise ValueError("A live job must have a locally stored committed revision.")
        return self


def parse_job_access(value: Any) -> JobAccess:
    return TypeAdapter(JobAccess).validate_python(value)
