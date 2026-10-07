"""Provider-neutral rendered-browser port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, cast
from uuid import UUID

from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionContext,
    BrowserDispatchConstraint,
    BrowserObservation,
    BrowserObservationExpansion,
    BrowserProviderError,
    BrowserSnapshot,
)
from agent_core.domain.browser_diagnostics import browser_phase_call
from agent_core.domain.browser_extraction import BrowserExtractionRequest
from agent_core.domain.tools import ToolExecutionContext


class BrowserProvider(Protocol):
    """A trusted-composition binding to one principal and browser profile."""

    name: str

    def allows(self, url: str) -> bool: ...

    async def navigate(self, url: str) -> BrowserObservation: ...

    async def observe(self) -> BrowserObservation: ...

    async def act(
        self,
        action: BrowserAction,
        *,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        """Dispatch one action; only a grant-authorized act names a constraint.

        The adapter's runtime rechecks the constraint against the live page and
        refuses with ``GRANT_NOT_APPLICABLE`` before dispatch (ADR-0129).
        """
        ...

    async def close(self) -> None: ...


async def expand_browser_observation(
    provider: object, request: BrowserObservationExpansion
) -> BrowserObservation:
    """Optional capability: a legacy provider refuses rather than dropping scope."""
    candidate = getattr(provider, "expand", None)
    if candidate is None:
        raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
    expand = cast(Callable[[BrowserObservationExpansion], Awaitable[BrowserObservation]], candidate)
    observation = await expand(request)
    if request.region_ref is not None and (
        observation.focus is None
        or observation.revision == request.expected_revision
        or observation.focus.text_offset != request.text_offset
    ):
        raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
    return observation


async def extract_browser_observation(
    provider: object, request: BrowserExtractionRequest
) -> BrowserObservation:
    """Optional capability: a legacy provider refuses rather than dropping scope."""
    candidate = getattr(provider, "extract", None)
    if candidate is None:
        raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
    extract = cast(Callable[[BrowserExtractionRequest], Awaitable[BrowserObservation]], candidate)
    observation = await extract(request)
    result = observation.extraction
    if (
        result is None
        or observation.revision == request.expected_revision
        or result.kind != request.kind
        or result.index != request.index
        or result.fields != request.fields
        or result.row_limit != request.row_limit
    ):
        raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
    return observation


# ADR-0129: the refusal a runtime gives when a grant's constraint does not
# hold on the live page; a pre-dispatch refusal.
GRANT_NOT_APPLICABLE = "tool.browser.grant_not_applicable"


@browser_phase_call("binding", "orchestrator")
async def bind_browser_execution(
    provider: BrowserProvider,
    context: ToolExecutionContext,
) -> None:
    """Bind execution scope when a hosted provider requires a lease."""

    candidate = getattr(provider, "bind_execution", None)
    if candidate is None:
        return
    binder = cast(Callable[[ToolExecutionContext], Awaitable[None]], candidate)
    await binder(context)


async def release_browser_run(provider: BrowserProvider, run_id: UUID) -> None:
    """Close the lease a hosted provider holds for a run that has ended."""

    candidate = getattr(provider, "release_run", None)
    if candidate is None:
        return
    releaser = cast(Callable[[UUID], Awaitable[None]], candidate)
    await releaser(run_id)


def browser_lease_upkeep(provider: BrowserProvider) -> Callable[[], Awaitable[None]] | None:
    """The periodic lease upkeep a hosted provider needs, if it holds leases."""

    candidate = getattr(provider, "maintain_leases", None)
    if candidate is None:
        return None
    return cast(Callable[[], Awaitable[None]], candidate)


async def browser_action_context(
    provider: BrowserProvider,
    action: BrowserAction,
) -> BrowserActionContext | None:
    candidate = getattr(provider, "action_context", None)
    if candidate is None:
        return None
    resolver = cast(Callable[[BrowserAction], Awaitable[BrowserActionContext]], candidate)
    return await resolver(action)


async def browser_snapshot_in_session(
    provider: BrowserProvider, session_id: UUID
) -> BrowserSnapshot | None:
    """The observation and facts a hosted provider cached for one session.

    Explicit by session, so an authorizer does not depend on which session's
    call bound the provider last (ADR-0129).
    """

    candidate = getattr(provider, "snapshot_in_session", None)
    if candidate is None:
        return None
    resolver = cast(Callable[[UUID], Awaitable[BrowserSnapshot | None]], candidate)
    return await resolver(session_id)


async def browser_action_context_in_session(
    provider: BrowserProvider, session_id: UUID, action: BrowserAction
) -> BrowserActionContext | None:
    """``action_context`` against one session's cached observation (ADR-0129)."""

    candidate = getattr(provider, "action_context_in_session", None)
    if candidate is None:
        return None
    resolver = cast(
        Callable[[UUID, BrowserAction], Awaitable[BrowserActionContext | None]], candidate
    )
    return await resolver(session_id, action)


async def check_browser_interruption(runtime: object) -> None:
    """Optional trusted challenge guard; older adapters retain their own refusal contract."""
    candidate = getattr(runtime, "check_automation_ready", None)
    if candidate is not None:
        await cast(Callable[[], Awaitable[None]], candidate)()


async def browser_action_interruption(
    runtime: object, observation: BrowserObservation
) -> BrowserObservation:
    """An action was dispatched; a subsequent sign-in screen is not effect proof."""
    try:
        await check_browser_interruption(runtime)
    except BrowserProviderError as error:
        if error.reason_code not in {
            "tool.browser.needs_user",
            "tool.browser.authentication_required",
        }:
            raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False) from error
        return BrowserObservation(
            url=observation.url,
            title=observation.title,
            revision=observation.revision,
            interruption="needs_user",
        )
    except Exception as error:
        raise BrowserProviderError("tool.browser.outcome_unknown", retryable=False) from error
    return observation
