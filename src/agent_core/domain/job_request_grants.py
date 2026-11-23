"""One approval for a request that changes a saved job (ADR-0344).

The approval the owner resolves with ``approve_for_request`` is the grant: it
belongs to the run that answers the owner's message, covers one job for at
most two hours, and is found again through the ``job.request_grant.created``
event that resolution appends. Its access ceilings are fixed when the card is
made; an action is covered only when one ceiling alone holds its access.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.cockpit import WIDGET_SLUG_PATTERN
from agent_core.domain.jobs import JobAccess
from agent_core.domain.sessions import (
    SESSION_COCKPIT_ENTRY_REFRESH_METADATA_KEY,
    SESSION_COCKPIT_REFRESH_METADATA_KEY,
    SESSION_DEVICE_TRIAGE_METADATA_KEY,
    SESSION_EMAIL_OPERATIONAL_METADATA_KEY,
    SESSION_PURPOSE_METADATA_KEY,
    SESSION_RUN_KIND_METADATA_KEY,
    SESSION_SCHEDULE_ID_METADATA_KEY,
)

JOB_REQUEST_GRANT_DURATION: Final = timedelta(hours=2)
JOB_REQUEST_GRANT_AUTHORIZATION_KIND: Final = "job_request_grant"
JOB_REQUEST_GRANT_AUTHORIZED_REASON: Final = "job.request_grant.authorized"
JOB_REQUEST_GRANT_CREATED_EVENT: Final = "job.request_grant.created"
# The denial when the run has no request approval for a job; it names no grant.
NO_JOB_REQUEST_GRANT: Final = "job.request_grant.none"
MAXIMUM_JOB_REQUEST_CEILINGS: Final = 16
JOB_REQUEST_GRANT_TOOLS: frozenset[str] = frozenset({"job.manage", "job.run", "schedule.update"})
# ADR-0344 decision 2: the job.manage operations a request grant covers.
JOB_REQUEST_MANAGE_OPERATIONS: frozenset[str] = frozenset({"trial", "update"})
# The schedule.update fields a repin may carry besides its task.
JOB_REQUEST_REPIN_FIELDS: frozenset[str] = frozenset(
    {"schedule_id", "expected_revision", "task", "title", "instruction"}
)
# A session carrying any of these is not the owner's own chat.
_NOT_OWNER_CHAT: Final = (
    SESSION_SCHEDULE_ID_METADATA_KEY,
    SESSION_RUN_KIND_METADATA_KEY,
    SESSION_EMAIL_OPERATIONAL_METADATA_KEY,
    SESSION_PURPOSE_METADATA_KEY,
    SESSION_DEVICE_TRIAGE_METADATA_KEY,
    SESSION_COCKPIT_REFRESH_METADATA_KEY,
    SESSION_COCKPIT_ENTRY_REFRESH_METADATA_KEY,
)


def session_is_owner_chat(metadata: Mapping[str, object]) -> bool:
    """Whether a session is the owner's interactive chat rather than a scheduled,
    inbound, device, email or refresh session."""

    return not any(key in metadata for key in _NOT_OWNER_CHAT)


def job_request_grant_summary(slug: str) -> str:
    """The server-authored offer text (ADR-0344 decision 6)."""

    return (
        f"Allow for this request: change, test and run the {slug} job and repin its "
        "schedule using access it already has. Veetbot still asks before new websites, "
        "MCP servers, feeds or larger limits, and before schedule timing changes."
    )


class JobAccessCeiling(BaseModel):
    """The most a covered action may use: one approved access without its
    private inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hosts: dict[str, str | None] = Field(default_factory=dict)
    mcp_servers: tuple[str, ...] = ()
    feeds: tuple[str, ...] = ()
    timeout_seconds: int = Field(ge=1)
    maximum_requests: int = Field(ge=1)
    maximum_response_bytes: int = Field(ge=1)
    model_cost: str

    @classmethod
    def from_access(cls, access: JobAccess) -> JobAccessCeiling:
        """The ceiling an approved access sets."""

        return cls(
            hosts=dict(sorted(access.hosts.items())),
            mcp_servers=tuple(sorted(access.mcp_servers)),
            feeds=tuple(sorted(access.feeds)),
            timeout_seconds=access.timeout_seconds,
            maximum_requests=access.maximum_requests,
            maximum_response_bytes=access.maximum_response_bytes,
            model_cost=str(Decimal(access.model_cost)),
        )

    def covers(self, access: JobAccess) -> bool:
        """Every destination is here with the same credential reference, and no
        limit is larger. Private inputs are not compared."""

        return (
            all(
                host in self.hosts and self.hosts[host] == credential
                for host, credential in access.hosts.items()
            )
            and access.mcp_servers <= set(self.mcp_servers)
            and access.feeds <= set(self.feeds)
            and access.timeout_seconds <= self.timeout_seconds
            and access.maximum_requests <= self.maximum_requests
            and access.maximum_response_bytes <= self.maximum_response_bytes
            and Decimal(access.model_cost) <= Decimal(self.model_cost)
        )


class JobRequestGrantOffer(BaseModel):
    """The server-authored offer stored on a job approval (ADR-0344)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str = Field(pattern=WIDGET_SLUG_PATTERN)
    ceilings: tuple[JobAccessCeiling, ...] = Field(
        min_length=1, max_length=MAXIMUM_JOB_REQUEST_CEILINGS
    )
    duration_seconds: Literal[7200] = 7200
    summary: str = Field(min_length=1, max_length=1024)

    def covers(self, access: JobAccess) -> bool:
        """Whether one ceiling alone holds the access; ceilings never merge."""

        return any(ceiling.covers(access) for ceiling in self.ceilings)
