"""Hosted provider binds model-visible tools to trusted profile leases."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.browser.hosted_provider import (
    HostedBrowserProvider,
    RunStateReader,
    SessionBoundHostedBrowserProvider,
)
from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserDispatchConstraint,
    BrowserElement,
    BrowserElementFacts,
    BrowserFieldKind,
    BrowserLease,
    BrowserObservation,
    BrowserObservationFacts,
    BrowserProfile,
    BrowserProfileStatus,
    BrowserProviderError,
    BrowserRunState,
    BrowserSnapshot,
)
from agent_core.domain.errors import NotFoundError
from agent_core.domain.tools import ToolExecutionContext
from agent_core.ports.browser import browser_action_context_in_session, browser_snapshot_in_session
from agent_core.ports.browser_sessions import BrowserSessionControlPlane
from agent_core.tools.browser_navigate import BrowserNavigateTool
from tests.contract.support import NOW, RUN_ID, SESSION_ID, principal, tool_context
from tests.contract.test_hosted_profile_session_service_contract import FakeSessionRuntime

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
    renewals: list[datetime] = field(default_factory=list)
    closes: list[str] = field(default_factory=list)
    sequence: list[int] = field(default_factory=list)
    act_started: asyncio.Event = field(default_factory=asyncio.Event)
    act_gate: asyncio.Event | None = None
    constraints: list[BrowserDispatchConstraint | None] = field(default_factory=list)

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
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        del lease_ref, action
        self.constraints.append(constraint)
        self.act_started.set()
        if self.act_gate is not None:
            await self.act_gate.wait()
        self.sequence.append(sequence)
        return BrowserObservation(url="https://example.org/current", revision="revision-2")

    async def renew(self, lease_ref: str, *, deadline_at: datetime) -> BrowserLease:
        self.renewals.append(deadline_at)
        return BrowserLease(lease_ref=lease_ref, expires_at=deadline_at)

    async def close(self, lease_ref: str) -> None:
        self.closes.append(lease_ref)


def lease_ref(acquisition: int, run_id: UUID = RUN_ID) -> str:
    return f"lease-reference-{run_id}-{acquisition:016d}"


def call_at(
    moment: datetime,
    *,
    session_id: UUID = SESSION_ID,
    run_id: UUID = RUN_ID,
    run_deadline_at: datetime | None = None,
) -> ToolExecutionContext:
    """One browser tool call, made at `moment`, of the contract run by default."""

    return replace(
        tool_context(),
        deadline_at=moment + timedelta(seconds=30),
        session_id=session_id,
        run_id=run_id,
        run_deadline_at=run_deadline_at,
    )


def run_states(states: dict[UUID, BrowserRunState]) -> RunStateReader:
    async def read(run_id: UUID) -> BrowserRunState:
        return states.get(run_id, BrowserRunState.ENDED)

    return read


CLICK = BrowserAction(
    kind=BrowserActionKind.CLICK,
    expected_revision="revision-1",
    ref="revision-1:0",
)


@dataclass
class LossySessions:
    """The real session service behind a link that can drop one answer."""

    service: HostedProfileSessionService
    lose: set[str] = field(default_factory=set)

    def _answer(self, operation: str) -> None:
        if operation in self.lose:
            self.lose.discard(operation)
            raise BrowserProviderError("tool.browser.provider_unavailable", retryable=True)

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
        lease = await self.service.acquire(
            profile_id,
            owner,
            provider_ref,
            run_id=run_id,
            attempt_number=attempt_number,
            deadline_at=deadline_at,
        )
        self._answer("acquire")
        return lease

    async def navigate(self, lease_ref: str, url: str) -> BrowserObservation:
        observation = await self.service.navigate(lease_ref, url)
        self._answer("navigate")
        return observation

    async def observe(self, lease_ref: str) -> BrowserObservation:
        observation = await self.service.observe(lease_ref)
        self._answer("observe")
        return observation

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        # The in-process service learns the constraint in Track R (ADR-0129 R2).
        assert constraint is None
        observation = await self.service.act(lease_ref, action, sequence=sequence)
        self._answer("act")
        return observation

    async def renew(self, lease_ref: str, *, deadline_at: datetime) -> BrowserLease:
        lease = await self.service.renew(lease_ref, deadline_at=deadline_at)
        self._answer("renew")
        return lease

    async def close(self, lease_ref: str) -> None:
        await self.service.close(lease_ref)


@dataclass
class HostedService:
    """The isolated session service over a real encrypted store, restartable."""

    store: FilesystemEncryptedProfileStore
    clock: list[datetime] = field(default_factory=lambda: [NOW])
    runtimes: list[FakeSessionRuntime] = field(default_factory=list)
    sessions: LossySessions = field(init=False)

    def __post_init__(self) -> None:
        self.sessions = LossySessions(self._service())

    def _service(self) -> HostedProfileSessionService:
        def runtime_factory(tenant_id: str) -> FakeSessionRuntime:
            assert tenant_id == principal().tenant_id
            runtime = FakeSessionRuntime()
            self.runtimes.append(runtime)
            return runtime

        return HostedProfileSessionService(
            self.store,
            runtime_factory=runtime_factory,
            now=self.now,
            process_secret=b"synthetic-provider-process-secret-32b",
            ceremony_base_url="https://browser-login.example.test",
        )

    def now(self) -> datetime:
        return self.clock[0]

    def restart(self) -> None:
        """A service restart invalidates every outstanding lease."""

        self.sessions.service = self._service()


async def hosted_service(tmp_path: Path) -> HostedService:
    store = FilesystemEncryptedProfileStore(
        tmp_path / "profiles",
        StaticProfileKeyring(
            {"key-v1": hashlib.sha256(b"synthetic-provider-key").digest()},
            current_version="key-v1",
        ),
    )
    await HostedProfileLifecycleService(store, reference_factory=lambda: PROVIDER_REF).provision(
        PROFILE_ID, principal(), ("https://example.org",)
    )
    return HostedService(store)


def ready_provider(
    sessions: BrowserSessionControlPlane,
    now: Callable[[], datetime] = lambda: NOW,
    run_state: RunStateReader | None = None,
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
        run_state=run_state,
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


@dataclass
class SettledPageSessions(FakeSessions):
    """Each act returns the page it settled on, with a new revision and refs."""

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        await super().act(lease_ref, action, sequence=sequence, constraint=constraint)
        revision = f"revision-{sequence + 1}"
        return BrowserObservation(
            url="https://example.org/lesson",
            revision=revision,
            elements=(BrowserElement(ref=f"{revision}:0", role="button", name="Continue"),),
        )


async def test_an_act_can_follow_an_act_on_its_returned_revision() -> None:
    """ADR-0130 decision 6 in hosted mode: act on the page act returned, no observe."""

    sessions = SettledPageSessions()
    provider = ready_provider(sessions)
    call = replace(tool_context(), deadline_at=NOW + timedelta(seconds=30))
    second = BrowserAction(
        kind=BrowserActionKind.CLICK, expected_revision="revision-2", ref="revision-2:0"
    )

    await provider.bind_execution(call)
    await provider.navigate("https://example.org/lesson")
    await provider.action_context(CLICK)
    returned = await provider.act(CLICK)
    context = await provider.action_context(second)
    await provider.act(second)

    assert returned.revision == "revision-2"
    assert (context.revision, context.ref) == ("revision-2", "revision-2:0")
    assert sessions.sequence == [1, 2]
    assert len(sessions.acquisitions) == 1


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
    # The service discards an expired lease itself, without sealing its state,
    # so the provider does not ask it to close one.
    assert sessions.closes == []


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


async def test_hosted_provider_reattaches_when_the_acquire_answer_is_lost(
    tmp_path: Path,
) -> None:
    hosted = await hosted_service(tmp_path)
    provider = ready_provider(hosted.sessions, now=hosted.now)
    hosted.sessions.lose.add("acquire")
    with pytest.raises(BrowserProviderError):
        await provider.bind_execution(call_at(NOW))
    # The retry comes later, so a chat run asks for a later fifteen-minute horizon.
    hosted.clock[0] = NOW + timedelta(seconds=40)

    await provider.bind_execution(call_at(hosted.now()))
    observation = await provider.navigate("https://example.org/lesson")
    await provider.release_run(RUN_ID)

    assert observation.url == "https://example.org/lesson"
    assert len(hosted.runtimes) == 1
    assert hosted.runtimes[0].closed


@pytest.mark.parametrize("failure", ["service_restart", "lost_answer"])
async def test_hosted_provider_drops_a_lease_it_can_no_longer_trust(
    tmp_path: Path,
    failure: str,
) -> None:
    hosted = await hosted_service(tmp_path)
    provider = ready_provider(hosted.sessions, now=hosted.now)
    await provider.bind_execution(call_at(NOW))
    await provider.navigate("https://example.org/lesson")
    if failure == "service_restart":
        hosted.restart()
    else:
        hosted.sessions.lose.add("observe")

    with pytest.raises(BrowserProviderError):
        await provider.observe()
    await provider.bind_execution(call_at(NOW + timedelta(seconds=10)))
    observation = await provider.navigate("https://example.org/lesson")

    assert observation.url == "https://example.org/lesson"
    assert len(hosted.runtimes) == 2


async def test_hosted_provider_retires_the_lease_when_an_action_answer_is_lost(
    tmp_path: Path,
) -> None:
    hosted = await hosted_service(tmp_path)
    provider = ready_provider(hosted.sessions, now=hosted.now)
    await provider.bind_execution(call_at(NOW))
    await provider.navigate("https://example.org/lesson")
    hosted.sessions.lose.add("act")

    with pytest.raises(BrowserProviderError) as raised:
        await provider.act(CLICK)
    assert raised.value.reason_code == "tool.browser.outcome_unknown"
    await provider.bind_execution(call_at(NOW + timedelta(seconds=10)))
    await provider.navigate("https://example.org/lesson")
    await provider.act(CLICK)

    # The lost action did land; the next one starts a fresh, in-step lease.
    assert [len(runtime.actions) for runtime in hosted.runtimes] == [1, 1]
    assert hosted.runtimes[0].closed


async def test_hosted_provider_retires_the_lease_when_an_action_is_abandoned() -> None:
    sessions = FakeSessions(act_gate=asyncio.Event())
    provider = ready_provider(sessions)
    await provider.bind_execution(call_at(NOW))
    await provider.navigate("https://example.org/lesson")
    acting = asyncio.create_task(provider.act(CLICK))
    await sessions.act_started.wait()

    acting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acting
    await provider.bind_execution(call_at(NOW + timedelta(seconds=10)))

    assert sessions.acquisitions == [(PROFILE_ID, RUN_ID, 1), (PROFILE_ID, RUN_ID, 1)]
    assert sessions.closes == [lease_ref(1)]


async def test_lease_upkeep_releases_a_lease_once_its_run_has_ended() -> None:
    sessions = FakeSessions()
    states = {RUN_ID: BrowserRunState.AWAITING_APPROVAL}
    provider = ready_provider(sessions, run_state=run_states(states))
    await provider.bind_execution(call_at(NOW))

    await provider.maintain_leases()
    assert sessions.closes == []
    # Cancelled while parked, by another process that cannot reach this lease.
    states[RUN_ID] = BrowserRunState.ENDED
    await provider.maintain_leases()

    assert sessions.closes == [lease_ref(1)]


async def test_lease_upkeep_renews_while_the_run_needs_the_browser_for_at_most_an_hour() -> None:
    sessions = FakeSessions()
    clock = [NOW]
    states = {RUN_ID: BrowserRunState.RUNNING}
    provider = ready_provider(sessions, now=lambda: clock[0], run_state=run_states(states))
    await provider.bind_execution(call_at(NOW))

    for minute in range(1, 71):
        clock[0] = NOW + timedelta(minutes=minute)
        if minute == 20:
            states[RUN_ID] = BrowserRunState.AWAITING_APPROVAL
        await provider.maintain_leases()

    # Renewed in fifteen-minute steps five minutes before each expiry, never
    # past sixty minutes; the expired lease is then dropped, not closed.
    assert sessions.renewals == [
        NOW + timedelta(minutes=minutes) for minutes in (25, 35, 45, 55, 60)
    ]
    assert sessions.closes == []
    await provider.bind_execution(call_at(clock[0]))
    assert len(sessions.acquisitions) == 2


async def test_lease_upkeep_never_renews_past_the_run_deadline() -> None:
    sessions = FakeSessions()
    clock = [NOW]
    provider = ready_provider(
        sessions,
        now=lambda: clock[0],
        run_state=run_states({RUN_ID: BrowserRunState.RUNNING}),
    )
    await provider.bind_execution(call_at(NOW, run_deadline_at=NOW + timedelta(minutes=20)))

    clock[0] = NOW + timedelta(minutes=11)
    await provider.maintain_leases()
    clock[0] = NOW + timedelta(minutes=16)
    await provider.maintain_leases()

    assert sessions.renewals == [NOW + timedelta(minutes=20)]


async def test_lease_upkeep_renews_while_an_approved_run_waits_in_the_queue() -> None:
    sessions = FakeSessions()
    clock = [NOW]
    provider = ready_provider(
        sessions,
        now=lambda: clock[0],
        run_state=run_states({RUN_ID: BrowserRunState.RESUMING}),
    )
    await provider.bind_execution(call_at(NOW))

    clock[0] = NOW + timedelta(minutes=11)
    await provider.maintain_leases()

    assert sessions.renewals == [NOW + timedelta(minutes=26)]


async def test_a_run_resumed_in_another_worker_continues_its_lease_and_action_sequence(
    tmp_path: Path,
) -> None:
    hosted = await hosted_service(tmp_path)
    first_worker = ready_provider(hosted.sessions, now=hosted.now)
    await first_worker.bind_execution(call_at(NOW))
    await first_worker.navigate("https://example.org/lesson")
    await first_worker.act(CLICK)
    # The first worker is lost; another worker's provider resumes the run.
    second_worker = ready_provider(hosted.sessions, now=hosted.now)

    await second_worker.bind_execution(call_at(NOW + timedelta(seconds=40)))
    await second_worker.act(
        BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision="revision-2",
            ref="revision-2:0",
        )
    )

    assert len(hosted.runtimes) == 1
    assert len(hosted.runtimes[0].actions) == 2


OTHER_RUN_ID = UUID("00000000-0000-0000-0000-0000000000eb")


@pytest.mark.parametrize(
    "holder",
    [
        BrowserRunState.RUNNING,
        BrowserRunState.RESUMING,
        BrowserRunState.AWAITING_APPROVAL,
    ],
)
async def test_another_run_cannot_take_the_page_of_a_run_that_still_needs_it(
    holder: BrowserRunState,
) -> None:
    sessions = FakeSessions()
    states = {RUN_ID: holder}
    provider = ready_provider(sessions, run_state=run_states(states))
    await provider.bind_execution(call_at(NOW))
    await provider.navigate("https://example.org/lesson")

    with pytest.raises(BrowserProviderError) as raised:
        await provider.bind_execution(call_at(NOW, run_id=OTHER_RUN_ID))
    assert raised.value.reason_code == "tool.browser.profile_unavailable"
    assert sessions.closes == []
    # Once the holder is done, the newcomer seals and replaces its lease.
    states[RUN_ID] = BrowserRunState.ENDED
    await provider.bind_execution(call_at(NOW, run_id=OTHER_RUN_ID))

    assert sessions.closes == [lease_ref(1)]
    assert sessions.acquisitions[-1] == (PROFILE_ID, OTHER_RUN_ID, 1)


async def test_another_run_replaces_a_holders_lease_once_it_has_expired() -> None:
    sessions = FakeSessions()
    clock = [NOW]
    provider = ready_provider(
        sessions,
        now=lambda: clock[0],
        run_state=run_states({RUN_ID: BrowserRunState.AWAITING_APPROVAL}),
    )
    await provider.bind_execution(call_at(NOW))
    clock[0] = NOW + LEASE_CAP

    await provider.bind_execution(call_at(clock[0], run_id=OTHER_RUN_ID))

    assert sessions.acquisitions[-1] == (PROFILE_ID, OTHER_RUN_ID, 1)


async def test_lease_upkeep_never_renews_the_lease_of_an_ended_run() -> None:
    sessions = FakeSessions()
    clock = [NOW]
    provider = ready_provider(
        sessions,
        now=lambda: clock[0],
        run_state=run_states({RUN_ID: BrowserRunState.ENDED}),
    )
    await provider.bind_execution(call_at(NOW))

    clock[0] = NOW + timedelta(minutes=11)
    await provider.maintain_leases()

    assert sessions.renewals == []
    assert sessions.closes == [lease_ref(1)]


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


async def test_session_bound_provider_releases_another_sessions_lease_once_its_run_ended() -> None:
    sessions = FakeSessions()
    states = {RUN_ID: BrowserRunState.AWAITING_APPROVAL}

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
        now=lambda: NOW,
        run_state=run_states(states),
    )
    await provider.bind_execution(call_at(NOW))
    # The parked run is cancelled from the API process; this worker's next
    # browser call, in another session, must not wait out the lease.
    states[RUN_ID] = BrowserRunState.ENDED
    await provider.bind_execution(
        call_at(
            NOW + timedelta(seconds=10),
            session_id=UUID("00000000-0000-0000-0000-0000000000e9"),
            run_id=UUID("00000000-0000-0000-0000-0000000000ea"),
        )
    )

    assert sessions.closes == [lease_ref(1)]


@dataclass
class RefusingSessions(FakeSessions):
    """The isolated runtime refuses a grant-authorized act before dispatch."""

    refusals: list[str] = field(default_factory=list)

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
        constraint: BrowserDispatchConstraint | None = None,
    ) -> BrowserObservation:
        if self.refusals:
            self.act_started.set()
            raise BrowserProviderError(self.refusals.pop(0), retryable=False)
        return await super().act(lease_ref, action, sequence=sequence, constraint=constraint)


async def test_grant_refusal_keeps_the_lease_and_sequence() -> None:
    """ADR-0129 (B2): grant_not_applicable is a pre-dispatch refusal."""

    sessions = RefusingSessions(refusals=["tool.browser.grant_not_applicable"])
    provider = ready_provider(sessions)
    await provider.bind_execution(call_at(NOW))
    await provider.navigate("https://example.org/lesson")

    with pytest.raises(BrowserProviderError) as refused:
        await provider.act(CLICK)

    assert (refused.value.reason_code, sessions.closes) == (
        "tool.browser.grant_not_applicable",
        [],
    )
    await provider.navigate("https://example.org/lesson")
    await provider.act(CLICK)
    assert len(sessions.acquisitions) == 1
    assert sessions.sequence == [1]


async def test_grant_refusal_discards_the_cached_observation() -> None:
    """D16: the model must observe again before it can act or be granted."""

    sessions = RefusingSessions(refusals=["tool.browser.grant_not_applicable"])
    provider = ready_provider(sessions)
    await provider.bind_execution(call_at(NOW))
    await provider.navigate("https://example.org/lesson")

    with pytest.raises(BrowserProviderError):
        await provider.act(CLICK)
    with pytest.raises(BrowserProviderError) as stale:
        await provider.action_context(CLICK)

    assert stale.value.reason_code == "tool.browser.page_changed"
    assert provider.snapshot() is None


FACTS = BrowserElementFacts(field_kind=BrowserFieldKind.NONE)


@dataclass
class FactSessions(FakeSessions):
    """A newer service: every page carries element facts beside it."""

    async def navigate(self, lease_ref: str, url: str) -> BrowserSnapshot:  # type: ignore[override]
        observation = await super().navigate(lease_ref, url)
        revision = f"revision-{url.rsplit('/', 1)[-1]}"
        observation = observation.model_copy(
            update={
                "revision": revision,
                "elements": (BrowserElement(ref=f"{revision}:0", role="button", name="Continue"),),
            }
        )
        return BrowserSnapshot(
            observation=observation,
            facts=BrowserObservationFacts(revision=revision, elements={f"{revision}:0": FACTS}),
        )


async def test_facts_are_cached_beside_the_observation_per_session() -> None:
    sessions = FactSessions()

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
        now=lambda: NOW,
    )
    other_session = UUID("00000000-0000-0000-0000-0000000000f5")
    first = call_at(NOW)
    second = call_at(NOW, session_id=other_session, run_id=UUID(int=0xF6))
    await provider.bind_execution(first)
    returned = await provider.navigate("https://example.org/a")
    await provider.bind_execution(second)
    await provider.navigate("https://example.org/b")

    mine = await browser_snapshot_in_session(provider, SESSION_ID)
    theirs = await browser_snapshot_in_session(provider, other_session)
    context = await browser_action_context_in_session(
        provider,
        SESSION_ID,
        BrowserAction(
            kind=BrowserActionKind.CLICK, expected_revision="revision-a", ref="revision-a:0"
        ),
    )

    assert isinstance(returned, BrowserObservation)
    assert mine is not None and theirs is not None
    assert (mine.observation.revision, theirs.observation.revision) == ("revision-a", "revision-b")
    assert mine.facts is not None and mine.facts.elements == {"revision-a:0": FACTS}
    assert context is not None and context.revision == "revision-a"
    assert await browser_snapshot_in_session(provider, UUID(int=0xF7)) is None


async def test_act_sends_the_constraint_in_the_request_body() -> None:
    """ADR-0129 section 8.4: the constraint rides in the act body, and only
    when a grant authorized the act."""

    bodies: list[dict[str, object]] = []

    def service(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith(":acquire"):
            return httpx.Response(
                200,
                json={
                    "lease_ref": "lease-reference-" + "1" * 32,
                    "expires_at": body["deadline_at"],
                },
            )
        page = {
            "url": "https://example.org/lesson",
            "revision": "revision-1",
            "elements": [{"ref": "revision-1:0", "role": "button", "name": "Continue"}],
        }
        if request.url.path.endswith(":act"):
            bodies.append(body)
        return httpx.Response(200, json=page)

    constraint = BrowserDispatchConstraint(
        grant_kind="task",
        origins=("https://example.org",),
        path_prefix="/lesson",
        not_after=NOW + timedelta(minutes=30),
        consequence_ceiling="unknown",
        max_text_characters=256,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        provider = ready_provider(
            HostedBrowserSessionControlPlane(
                base_url="https://browser.example.test",
                credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
                client=client,
            )
        )
        await provider.bind_execution(call_at(NOW))
        await provider.navigate("https://example.org/lesson")
        await provider.act(CLICK, constraint=constraint)
        await provider.act(CLICK)

    assert [body.get("constraint") for body in bodies] == [
        constraint.model_dump(mode="json"),
        None,
    ]
    assert "constraint" not in bodies[1]
    assert [body["sequence"] for body in bodies] == [1, 2]
