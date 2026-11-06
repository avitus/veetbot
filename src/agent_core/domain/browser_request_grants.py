"""One approval for a request on a signed-in website (ADR-0302).

The approval that the owner resolves with ``approve_for_request`` is the
grant: it belongs to the run that answers the owner's message, covers the
login's origins for at most two hours, and is found again through the
``browser.request_grant.created`` event that resolution appends. No table,
counter or route of its own exists; the run's budgets bound its actions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_core.domain.browser import (
    BrowserAction,
    BrowserProfileStatus,
    normalize_browser_origin,
)
from agent_core.domain.browser_act_views import (
    BROWSER_TOOL_NAMES,
    BrowserActView,
    TaskGrantSessionContext,
    request_grant_view_coverage,
    session_is_task_grant_eligible,
)
from agent_core.domain.policies import AuthorizationTurn
from agent_core.domain.runs import Run
from agent_core.domain.sessions import SESSION_BROWSER_PROFILE_METADATA_KEY

REQUEST_GRANT_DURATION: Final = timedelta(hours=2)
REQUEST_GRANT_AUTHORIZATION_KIND: Final = "request_grant"
REQUEST_GRANT_AUTHORIZED_REASON: Final = "browser.request_grant.authorized"
REQUEST_GRANT_CREATED_EVENT: Final = "browser.request_grant.created"
# The denial when the run has no request approval; it names no grant.
NO_REQUEST_GRANT: Final = "browser.request_grant.none"
MAXIMUM_REQUEST_GRANT_ORIGINS: Final = 64

# ADR-0302 decision 3: tools that add nothing a page could be typed from, so
# they never count as an outside read. ``artifact.export`` and an answered
# question are already left out of the turn's tool names (ADR-0263).
REQUEST_GRANT_BOOKKEEPING_TOOLS: frozenset[str] = frozenset(
    {
        "system.current_time",
        "context.update_working_state",
        "math.calculate",
        "artifact.export",
        "artifact.read_capture",
    }
)


def turn_reads_outside(turn: AuthorizationTurn | None) -> bool:
    """Whether the active turn read mail, memory, knowledge, the web or
    anything else besides the browser and run bookkeeping. An unknown turn
    counts as one."""

    if turn is None:
        return True
    return bool(turn.tool_names - BROWSER_TOOL_NAMES - REQUEST_GRANT_BOOKKEEPING_TOOLS)


def request_grant_summary(origins: tuple[str, ...]) -> str:
    """The server-authored offer text (ADR-0302 decision 6)."""

    hosts = ", ".join(origin.removeprefix("https://") for origin in origins)
    return (
        "Allow for this request: open pages, choose menus and filters, type dates "
        f"and searches, and save downloaded files into this chat on {hosts}. "
        "Veetbot still asks before payments, transfers, messages, account or "
        "security changes, deletions, uploads and passwords. It recognises those "
        "by the website's labels."
    )[:1024]


class BrowserRequestGrantOffer(BaseModel):
    """The server-authored offer stored on a ``browser.act`` approval.

    The origins and generation are the profile's when the card was made,
    never values taken from the page.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: UUID
    profile_generation: int = Field(ge=0)
    origins: tuple[str, ...] = Field(min_length=1, max_length=MAXIMUM_REQUEST_GRANT_ORIGINS)
    duration_seconds: Literal[7200] = 7200
    summary: str = Field(min_length=1, max_length=1024)

    @field_validator("origins")
    @classmethod
    def origins_are_exact_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize each origin and refuse a repeated one."""

        normalized = tuple(normalize_browser_origin(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("request grant origins must be unique")
        return normalized


class RequestGrantEcho(BaseModel):
    """The offered profile, repeated in an ``approve_for_request`` body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: UUID


def request_grant_offer(
    *,
    view: BrowserActView,
    action: BrowserAction,
    run: Run,
    tenant_id: str,
    principal_id: str,
    turn: AuthorizationTurn | None,
    context: TaskGrantSessionContext,
) -> BrowserRequestGrantOffer | None:
    """The card's offer, or none: an owner-driven run of a chat bound to a
    READY login, and an action the grant would itself cover (ADR-0302)."""

    session, profile = context.session, context.profile
    if (
        not view.described
        or view.observation is None
        or view.element is None
        or session is None
        or profile is None
        or profile.status is not BrowserProfileStatus.READY
        or session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY) != str(profile.id)
        or not session_is_task_grant_eligible(
            run,
            session_tenant_id=session.tenant_id,
            session_principal_id=session.principal_id,
            session_metadata=session.metadata,
            owner_run_id=context.owner_run_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
            turn=turn,
        )
    ):
        return None
    origins = tuple(dict.fromkeys(profile.allowed_origins))[:MAXIMUM_REQUEST_GRANT_ORIGINS]
    if not origins:
        return None
    coverage = request_grant_view_coverage(
        view, action, origins=origins, free_text=not turn_reads_outside(turn)
    )
    if not coverage.covered:
        return None
    return BrowserRequestGrantOffer(
        profile_id=profile.id,
        profile_generation=profile.generation,
        origins=origins,
        summary=request_grant_summary(origins),
    )
