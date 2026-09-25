"""Hosted provider binds model-visible tools to trusted profile leases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from agent_core.adapters.browser.hosted_provider import (
    HostedBrowserProvider,
    SessionBoundHostedBrowserProvider,
)
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserElement,
    BrowserLease,
    BrowserObservation,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
)
from agent_core.domain.errors import NotFoundError
from agent_core.domain.tools import ToolExecutionContext
from agent_core.tools.browser_navigate import BrowserNavigateTool
from tests.contract.support import NOW, principal, tool_context

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000e7")
PROVIDER_REF = "opaque-hosted-provider-reference-0000000001"
LEASE_CAP = timedelta(minutes=15)


def profile(status: BrowserProfileStatus = BrowserProfileStatus.READY) -> BrowserProfile:
    return BrowserProfile(
        id=PROFILE_ID,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        provider_name="hosted-isolated",
        provider_ref=PROVIDER_REF,
        allowed_origins=("https://example.org",),
        status=status,
        generation=2,
        encryption_key_version="key-v1",
        created_at=NOW,
        updated_at=NOW,
    )


@dataclass
class FakeSessions:
    acquisitions: list[tuple[UUID, UUID, int]] = field(default_factory=list)
    deadlines: list[datetime] = field(default_factory=list)
    closes: list[str] = field(default_factory=list)
    sequence: list[int] = field(default_factory=list)

    async def acquire(
        self,
        profile_id: UUID,
        owner: Principal,
        provider_ref: str,
        *,
        run_id: UUID,
        attempt_number: int,
        deadline_at: datetime,
    ) -> BrowserLease:
        del owner, provider_ref
        self.acquisitions.append((profile_id, run_id, attempt_number))
        self.deadlines.append(deadline_at)
        return BrowserLease(
            lease_ref=f"lease-reference-{run_id}-{len(self.acquisitions):016d}",
            expires_at=deadline_at,
        )

    async def navigate(self, lease_ref: str, url: str) -> BrowserObservation:
        del lease_ref
        return BrowserObservation(
            url=url,
            revision="revision-1",
            elements=(BrowserElement(ref="revision-1:0", role="button", name="Continue"),),
        )

    async def observe(self, lease_ref: str) -> BrowserObservation:
        del lease_ref
        return BrowserObservation(url="https://example.org/current", revision="revision-1")

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
    ) -> BrowserObservation:
        del lease_ref, action
        self.sequence.append(sequence)
        return BrowserObservation(url="https://example.org/current", revision="revision-2")

    async def close(self, lease_ref: str) -> None:
        self.closes.append(lease_ref)


def ready_provider(
    sessions: FakeSessions,
    now: Callable[[], datetime] = lambda: NOW,
) -> HostedBrowserProvider:
    async def load(owner: Principal, profile_id: UUID) -> BrowserProfile:
        assert owner == principal()
        assert profile_id == PROFILE_ID
        return profile()

    return HostedBrowserProvider(
        principal=principal(),
        profile_id=PROFILE_ID,
        allowed_origins=("https://example.org",),
        profiles=load,
        sessions=sessions,
        now=now,
    )


async def test_hosted_provider_keeps_one_lease_per_run_attempt_and_rotates_between_attempts() -> (
    None
):
    sessions = FakeSessions()
    provider = ready_provider(sessions)
    first = replace(tool_context(), deadline_at=NOW + timedelta(minutes=5))
    extended = replace(first, deadline_at=first.deadline_at + timedelta(minutes=1))
    second = replace(
        first,
        run_id=UUID("00000000-0000-0000-0000-0000000000e8"),
        attempt_number=2,
    )

    await provider.bind_execution(first)
    await provider.navigate("https://example.org/lesson")
    await provider.bind_execution(first)
    await provider.act(
        BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision="revision-1",
            ref="revision-1:0",
        )
    )
    await provider.bind_execution(extended)
    await provider.act(
        BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision="revision-2",
            ref="revision-2:0",
        )
    )
    await provider.bind_execution(second)

    # A later call of the same attempt keeps the lease and its action sequence;
    # only a new run attempt replaces it.
    assert sessions.acquisitions == [
        (PROFILE_ID, first.run_id, 1),
        (PROFILE_ID, second.run_id, 2),
    ]
    assert sessions.deadlines == [NOW + LEASE_CAP, NOW + LEASE_CAP]
    assert sessions.sequence == [1, 2]
    assert len(sessions.closes) == 1


async def test_hosted_provider_keeps_the_run_attempt_lease_from_navigation_to_action() -> None:
    """A later call's own deadline must not replace the browser the run attempt opened."""
    sessions = FakeSessions()
    provider = ready_provider(sessions)
    navigate_call = replace(tool_context(), deadline_at=NOW + timedelta(seconds=30))
    act_call = replace(
        navigate_call,
        invocation_id=UUID(int=71),
        call_id="call-act",
        step_number=2,
        deadline_at=NOW + timedelta(minutes=2, seconds=30),
    )
    action = BrowserAction(
        kind=BrowserActionKind.CLICK,
        expected_revision="revision-1",
        ref="revision-1:0",
    )

    await provider.bind_execution(navigate_call)
    await provider.navigate("https://example.org/lesson")
    await provider.bind_execution(act_call)
    context = await provider.action_context(action)
    await provider.act(action)

    assert context.revision == "revision-1"
    assert sessions.acquisitions == [(PROFILE_ID, navigate_call.run_id, 1)]
    assert sessions.closes == []
    assert sessions.sequence == [1]


async def test_hosted_provider_bounds_the_lease_by_a_nearer_run_deadline() -> None:
    sessions = FakeSessions()
    provider = ready_provider(sessions)
    run_deadline = NOW + timedelta(minutes=4)

    await provider.bind_execution(
        replace(
            tool_context(),
            deadline_at=NOW + timedelta(seconds=30),
            run_deadline_at=run_deadline,
        )
    )

    assert sessions.deadlines == [run_deadline]


async def test_hosted_provider_replaces_an_expired_lease_without_renewing_it() -> None:
    sessions = FakeSessions()
    clock = [NOW]
    provider = ready_provider(sessions, now=lambda: clock[0])

    await provider.bind_execution(replace(tool_context(), deadline_at=NOW + timedelta(seconds=30)))
    clock[0] = NOW + LEASE_CAP
    await provider.bind_execution(
        replace(tool_context(), deadline_at=clock[0] + timedelta(seconds=30))
    )

    assert sessions.acquisitions == [
        (PROFILE_ID, tool_context().run_id, 1),
        (PROFILE_ID, tool_context().run_id, 1),
    ]
    assert sessions.deadlines == [NOW + LEASE_CAP, NOW + 2 * LEASE_CAP]
    assert len(sessions.closes) == 1


async def test_hosted_provider_releases_only_the_lease_of_the_run_that_ended() -> None:
    sessions = FakeSessions()
    provider = ready_provider(sessions)
    call = replace(tool_context(), deadline_at=NOW + timedelta(seconds=30))

    await provider.bind_execution(call)
    await provider.release_run(UUID("00000000-0000-0000-0000-0000000000e8"))
    assert sessions.closes == []

    await provider.release_run(call.run_id)
    await provider.release_run(call.run_id)
    assert len(sessions.closes) == 1
    with pytest.raises(BrowserProviderError) as raised:
        await provider.observe()
    assert raised.value.reason_code == "tool.browser.profile_unavailable"


@pytest.mark.parametrize(
    "status,reason",
    [
        (BrowserProfileStatus.AUTHENTICATION_REQUIRED, "tool.browser.authentication_required"),
        (BrowserProfileStatus.NEEDS_USER, "tool.browser.needs_user"),
        (BrowserProfileStatus.REVOKED, "tool.browser.profile_unavailable"),
    ],
)
async def test_hosted_provider_refuses_non_ready_profile(
    status: BrowserProfileStatus,
    reason: str,
) -> None:
    async def load(owner: Principal, profile_id: UUID) -> BrowserProfile:
        del owner, profile_id
        return profile(status)

    provider = HostedBrowserProvider(
        principal=principal(),
        profile_id=PROFILE_ID,
        allowed_origins=("https://example.org",),
        profiles=load,
        sessions=FakeSessions(),
        now=lambda: NOW,
    )

    with pytest.raises(BrowserProviderError) as raised:
        await provider.bind_execution(
            replace(tool_context(), deadline_at=NOW + timedelta(minutes=5))
        )

    assert raised.value.reason_code == reason


async def test_hosted_provider_normalizes_missing_profile_without_leaking_repository_state() -> (
    None
):
    async def load(owner: Principal, profile_id: UUID) -> BrowserProfile:
        del owner, profile_id
        raise NotFoundError("browser profile not found")

    provider = HostedBrowserProvider(
        principal=principal(),
        profile_id=PROFILE_ID,
        allowed_origins=("https://example.org",),
        profiles=load,
        sessions=FakeSessions(),
        now=lambda: NOW,
    )

    with pytest.raises(BrowserProviderError) as raised:
        await provider.bind_execution(
            replace(tool_context(), deadline_at=NOW + timedelta(minutes=5))
        )

    assert raised.value.reason_code == "tool.browser.profile_unavailable"
    assert raised.value.retryable is False


async def test_session_bound_provider_resolves_profile_before_enforcing_its_origins() -> None:
    sessions = FakeSessions()
    selected_contexts: list[ToolExecutionContext] = []

    async def load(owner: Principal, profile_id: UUID) -> BrowserProfile:
        assert owner == principal()
        assert profile_id == PROFILE_ID
        return profile()

    async def select(context: ToolExecutionContext) -> UUID:
        selected_contexts.append(context)
        return PROFILE_ID

    provider = SessionBoundHostedBrowserProvider(
        principal=principal(),
        profiles=load,
        profile_selector=select,
        sessions=sessions,
        now=lambda: NOW,
    )
    context = replace(tool_context(), deadline_at=NOW + timedelta(minutes=5))

    result = await BrowserNavigateTool(provider).execute(
        {"url": "https://example.org/account"},
        context,
    )

    assert result.ok
    assert selected_contexts == [context]
    assert sessions.acquisitions == [(PROFILE_ID, context.run_id, context.attempt_number)]


async def test_session_bound_provider_keeps_a_live_run_lease_until_that_run_ends() -> None:
    sessions = FakeSessions()
    clock = [NOW]

    async def load(owner: Principal, profile_id: UUID) -> BrowserProfile:
        del owner, profile_id
        return profile()

    async def select(context: ToolExecutionContext) -> UUID:
        del context
        return PROFILE_ID

    provider = SessionBoundHostedBrowserProvider(
        principal=principal(),
        profiles=load,
        profile_selector=select,
        sessions=sessions,
        now=lambda: clock[0],
    )
    first = replace(tool_context(), deadline_at=NOW + timedelta(seconds=30))
    await provider.bind_execution(first)
    # Another session's call arrives after the first call's own deadline.
    clock[0] = NOW + timedelta(minutes=2)
    await provider.bind_execution(
        replace(
            tool_context(),
            session_id=UUID("00000000-0000-0000-0000-0000000000e9"),
            run_id=UUID("00000000-0000-0000-0000-0000000000ea"),
            deadline_at=clock[0] + timedelta(seconds=30),
        )
    )
    assert sessions.closes == []

    await provider.release_run(first.run_id)

    assert sessions.closes == [f"lease-reference-{first.run_id}-{1:016d}"]
