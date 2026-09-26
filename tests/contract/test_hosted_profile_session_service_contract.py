"""Exclusive hosted browser leases and direct authentication ceremonies."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest

from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.handoff import DeviceSessionHandoff
from agent_core.browser_control_plane.models import (
    ProfileMaterialIdentity,
    ProfileMaterialMetadata,
)
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import (
    CeremonyCapabilityRejected,
    DeviceHandoffInvalid,
    DeviceSessionRejected,
    HostedProfileSessionService,
)
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationMode,
    BrowserAuthenticationStatus,
    BrowserInteractiveEvent,
    BrowserLease,
    BrowserObservation,
    BrowserPageEvidence,
    BrowserProviderError,
)
from agent_core.domain.errors import ConflictError
from tests.contract.support import NOW, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000f4")
RUN_ID = UUID("00000000-0000-0000-0000-0000000000f5")
PROVIDER_REF = "opaque-session-reference-0000000000000001"


NO_SESSION = b'{"format_version":1}'
SIGNED_OUT = BrowserPageEvidence(on_allowed_origin=True, path="/log-in", challenge_visible=True)


@dataclass
class HandoffScenario:
    """What a device handoff's two verification loads see (ADR-0128 section 4.4)."""

    with_session: BrowserPageEvidence | BaseException | str | None = None
    without_session: BrowserPageEvidence | BaseException | str | None = SIGNED_OUT
    start_error: Exception | None = None
    storage_error: Exception | None = None
    gate: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class FakeSessionRuntime:
    initial_material: bytes | None = None
    allowed_origins: tuple[str, ...] = ()
    interactive: bool = False
    sealed_material: bytes = b'{"format_version":1,"sealed":true}'
    authentication: BrowserAuthenticationStatus = BrowserAuthenticationStatus.READY
    closed: bool = False
    actions: list[BrowserAction] = field(default_factory=list)
    observe_started: asyncio.Event | None = None
    observe_release: asyncio.Event | None = None
    navigate_error: Exception | None = None
    # Device-handoff verification: what each load shows, keyed by whether the
    # runtime started with a session, and the failures a scenario injects.
    scenario: HandoffScenario | None = None
    evidence_urls: list[str] = field(default_factory=list)

    async def start(
        self,
        material: bytes,
        allowed_origins: tuple[str, ...],
        *,
        interactive: bool,
    ) -> None:
        self.initial_material = material
        self.allowed_origins = allowed_origins
        self.interactive = interactive
        if self.scenario is not None and self.scenario.start_error is not None:
            raise self.scenario.start_error

    async def navigate(self, url: str) -> BrowserObservation:
        """Simulate the browser navigation result needed by this failure-path regression."""
        if self.navigate_error is not None:
            raise self.navigate_error
        return BrowserObservation(url=url, revision="revision-1", text="safe observation")

    async def observe(self) -> BrowserObservation:
        if self.observe_started is not None:
            self.observe_started.set()
        if self.observe_release is not None:
            await self.observe_release.wait()
        return BrowserObservation(
            url=self.allowed_origins[0] + "/current",
            revision="revision-1",
        )

    async def act(self, action: BrowserAction) -> BrowserObservation:
        self.actions.append(action)
        return BrowserObservation(
            url=self.allowed_origins[0] + "/current",
            revision="revision-2",
        )

    @property
    def signed_in(self) -> bool:
        return self.initial_material not in {None, NO_SESSION}

    async def load_page_evidence(self, url: str) -> BrowserPageEvidence:
        self.evidence_urls.append(url)
        scenario = self.scenario or HandoffScenario()
        if scenario.gate is not None:
            scenario.started.set()
            await scenario.gate.wait()
        outcome = scenario.with_session if self.signed_in else scenario.without_session
        if outcome == "hang":
            await asyncio.sleep(3600)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            return BrowserPageEvidence(
                on_allowed_origin=True, path=urlsplit(url).path or "/", challenge_visible=False
            )
        assert isinstance(outcome, BrowserPageEvidence)
        return outcome

    async def storage_state(self) -> bytes:
        if self.scenario is not None and self.scenario.storage_error is not None:
            raise self.scenario.storage_error
        return self.sealed_material

    async def authentication_status(self) -> BrowserAuthenticationStatus:
        return self.authentication

    async def interactive_frame(self) -> bytes:
        return b"synthetic-png-frame"

    async def interactive_event(self, event: BrowserInteractiveEvent) -> None:
        del event

    async def close(self) -> None:
        self.closed = True


def services(
    tmp_path: Path,
    *,
    first_navigate_error: Exception | None = None,
    scenario: HandoffScenario | None = None,
    verification_seconds: float = 30.0,
) -> tuple[
    HostedProfileLifecycleService,
    HostedProfileSessionService,
    list[FakeSessionRuntime],
    list[datetime],
]:
    """Build isolated hosted-session fixtures for the shared authentication contract."""
    keyring = StaticProfileKeyring(
        {"key-v1": hashlib.sha256(b"synthetic-session-key").digest()},
        current_version="key-v1",
    )
    store = FilesystemEncryptedProfileStore(tmp_path / "profiles", keyring)
    runtimes: list[FakeSessionRuntime] = []
    times = [NOW]

    def runtime_factory(tenant_id: str) -> FakeSessionRuntime:
        """Create and retain a fake browser runtime for lifecycle assertions."""
        assert tenant_id == principal().tenant_id
        runtime = FakeSessionRuntime(
            navigate_error=first_navigate_error if not runtimes else None,
            scenario=scenario,
        )
        runtimes.append(runtime)
        return runtime

    sessions = HostedProfileSessionService(
        store,
        runtime_factory=runtime_factory,
        now=lambda: times[0],
        process_secret=b"synthetic-process-secret-with-32-bytes",
        ceremony_base_url="https://browser-login.example.test",
        verification_seconds=verification_seconds,
    )
    lifecycle = HostedProfileLifecycleService(
        store,
        reference_factory=lambda: PROVIDER_REF,
        invalidate_profile=sessions.invalidate_profile,
    )
    return lifecycle, sessions, runtimes, times


async def provision(lifecycle: HostedProfileLifecycleService) -> None:
    await lifecycle.provision(PROFILE_ID, principal(), ("https://example.org",))


async def test_hosted_session_lease_is_scoped_exclusive_and_seals_server_side(
    tmp_path: Path,
) -> None:
    lifecycle, sessions, runtimes, _times = services(tmp_path)
    await provision(lifecycle)

    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=10),
    )
    replay = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=10),
    )
    with pytest.raises(ConflictError):
        await sessions.acquire(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            run_id=UUID("00000000-0000-0000-0000-0000000000f6"),
            attempt_number=1,
            deadline_at=NOW + timedelta(minutes=10),
        )

    observed = await sessions.navigate(lease.lease_ref, "https://example.org/lesson")
    await sessions.act(
        lease.lease_ref,
        BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision="revision-1",
            ref="revision-1:0",
        ),
        sequence=1,
    )
    await sessions.close(lease.lease_ref)

    assert replay.lease_ref == lease.lease_ref
    assert observed.url == "https://example.org/lesson"
    assert runtimes[0].initial_material is not None
    assert runtimes[0].closed is True
    assert PROVIDER_REF not in repr(lease)


async def test_revocation_fences_and_closes_live_lease(tmp_path: Path) -> None:
    lifecycle, sessions, runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=10),
    )

    await lifecycle.revoke(PROFILE_ID, principal(), PROVIDER_REF)

    assert runtimes[0].closed is True
    with pytest.raises(BrowserProviderError) as raised:
        await sessions.observe(lease.lease_ref)
    assert raised.value.reason_code == "tool.browser.profile_unavailable"


async def test_expired_lease_fails_without_sealing_client_material(tmp_path: Path) -> None:
    lifecycle, sessions, runtimes, times = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(seconds=1),
    )
    times[0] = NOW + timedelta(seconds=2)

    with pytest.raises(BrowserProviderError) as raised:
        await sessions.observe(lease.lease_ref)

    assert raised.value.reason_code == "tool.browser.profile_unavailable"
    assert runtimes[-1].closed is True


async def test_expired_lease_waits_for_an_active_operation_before_closing(
    tmp_path: Path,
) -> None:
    lifecycle, sessions, runtimes, times = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(seconds=1),
    )
    runtime = runtimes[-1]
    runtime.observe_started = asyncio.Event()
    runtime.observe_release = asyncio.Event()
    active = asyncio.create_task(sessions.observe(lease.lease_ref))
    await runtime.observe_started.wait()
    times[0] = NOW + timedelta(seconds=2)

    expiry = asyncio.create_task(sessions.observe(lease.lease_ref))
    await asyncio.sleep(0)

    assert runtime.closed is False
    runtime.observe_release.set()
    await active
    with pytest.raises(BrowserProviderError):
        await expiry
    assert runtime.closed is True


async def test_repeated_acquire_for_the_same_attempt_reattaches_to_its_live_lease(
    tmp_path: Path,
) -> None:
    """A retry whose first response was lost gets the live lease, not a conflict."""
    lifecycle, sessions, runtimes, times = services(tmp_path)
    await provision(lifecycle)
    first = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=15),
    )
    times[0] = NOW + timedelta(seconds=40)

    retried = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=times[0] + timedelta(minutes=15),
    )

    assert retried == first
    assert len(runtimes) == 1
    with pytest.raises(ConflictError):
        await sessions.acquire(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            run_id=RUN_ID,
            attempt_number=2,
            deadline_at=times[0] + timedelta(minutes=15),
        )


async def test_reattaching_to_a_lease_continues_its_action_sequence(tmp_path: Path) -> None:
    lifecycle, sessions, runtimes, times = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=15),
    )
    await sessions.act(
        lease.lease_ref,
        BrowserAction(
            kind=BrowserActionKind.CLICK,
            expected_revision="revision-1",
            ref="revision-1:0",
        ),
        sequence=1,
    )
    times[0] = NOW + timedelta(seconds=40)

    reattached = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=times[0] + timedelta(minutes=15),
    )

    assert lease.sequence == 0
    assert reattached.lease_ref == lease.lease_ref
    assert reattached.sequence == 1
    assert len(runtimes) == 1


async def test_closing_an_expired_lease_does_not_seal_its_state(tmp_path: Path) -> None:
    lifecycle, sessions, runtimes, times = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(seconds=1),
    )
    times[0] = NOW + timedelta(seconds=2)

    # A queued close retried after expiry reaches the service before any sweep.
    await sessions.close(lease.lease_ref)
    await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=UUID("00000000-0000-0000-0000-0000000000f6"),
        attempt_number=1,
        deadline_at=times[0] + timedelta(minutes=5),
    )

    assert runtimes[0].closed is True
    assert runtimes[1].initial_material == runtimes[0].initial_material
    assert runtimes[1].initial_material != runtimes[0].sealed_material


async def test_lease_renews_in_fifteen_minute_steps_for_at_most_an_hour(
    tmp_path: Path,
) -> None:
    lifecycle, sessions, runtimes, times = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=15),
    )

    times[0] = NOW + timedelta(minutes=10)
    first = await sessions.renew(lease.lease_ref, deadline_at=NOW + timedelta(hours=2))
    expiries = [first.expires_at]
    for minute in (20, 30, 40, 50, 55):
        times[0] = NOW + timedelta(minutes=minute)
        renewed = await sessions.renew(lease.lease_ref, deadline_at=NOW + timedelta(hours=2))
        expiries.append(renewed.expires_at)
    await sessions.observe(lease.lease_ref)

    # Each request adds at most fifteen minutes; the lease never outlives an hour.
    assert first == BrowserLease(lease_ref=lease.lease_ref, expires_at=NOW + timedelta(minutes=25))
    assert expiries == [NOW + timedelta(minutes=minutes) for minutes in (25, 35, 45, 55, 60, 60)]
    times[0] = NOW + timedelta(minutes=60)
    with pytest.raises(BrowserProviderError) as raised:
        await sessions.renew(lease.lease_ref, deadline_at=NOW + timedelta(hours=2))
    assert raised.value.reason_code == "tool.browser.profile_unavailable"
    assert runtimes[0].closed is True


async def test_lease_renewal_is_refused_once_the_profile_is_revoked(tmp_path: Path) -> None:
    lifecycle, sessions, _runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    lease = await sessions.acquire(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        run_id=RUN_ID,
        attempt_number=1,
        deadline_at=NOW + timedelta(minutes=15),
    )

    await lifecycle.revoke(PROFILE_ID, principal(), PROVIDER_REF)

    with pytest.raises(BrowserProviderError) as raised:
        await sessions.renew(lease.lease_ref, deadline_at=NOW + timedelta(minutes=30))
    assert raised.value.reason_code == "tool.browser.profile_unavailable"


async def assert_authentication_ceremony_is_direct_single_use_and_runtime_decided(
    tmp_path: Path,
    runtime_status: BrowserAuthenticationStatus,
) -> None:
    lifecycle, sessions, runtimes, _times = services(tmp_path)
    await provision(lifecycle)

    ceremony = await sessions.begin_authentication(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        login_url="https://example.org/login",
    )
    runtimes[0].authentication = runtime_status
    public_before = await sessions.authentication_status(ceremony.id, principal())
    result = await sessions.refresh_authentication(ceremony.id, principal())

    assert ceremony.launch_url is not None
    assert "#capability=" in ceremony.launch_url
    assert public_before.launch_url is None
    assert result.status is runtime_status
    assert "capability" not in result.model_dump_json()
    if runtime_status is BrowserAuthenticationStatus.READY:
        assert runtimes[0].closed is True


@pytest.mark.parametrize(
    "runtime_status",
    [
        BrowserAuthenticationStatus.READY,
        BrowserAuthenticationStatus.NEEDS_USER,
        BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
    ],
)
async def test_authentication_ceremony_is_direct_single_use_and_runtime_decided(
    tmp_path: Path,
    runtime_status: BrowserAuthenticationStatus,
) -> None:
    await assert_authentication_ceremony_is_direct_single_use_and_runtime_decided(
        tmp_path,
        runtime_status=runtime_status,
    )


async def test_restarted_service_never_reissues_a_ceremony_capability(tmp_path: Path) -> None:
    """A capability that can write profile material must never repeat (ADR-0128 D3).

    A restarted service keeps its store and process secret, so a capability
    derived from them and a per-process counter would be issued again.
    """

    lifecycle, first, _runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    before = await first.begin_authentication(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        login_url="https://example.org/login",
    )
    await first.cancel_authentication(before.id, principal())
    _lifecycle, restarted, _restarted_runtimes, _restarted_times = services(tmp_path)

    after = await restarted.begin_authentication(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        login_url="https://example.org/login",
    )

    assert before.launch_url is not None and after.launch_url is not None
    before_capability = before.launch_url.split("#capability=", 1)[1]
    after_capability = after.launch_url.split("#capability=", 1)[1]
    assert before_capability != after_capability
    assert before.id != after.id
    assert len(after_capability) == 43


async def test_authentication_scope_mismatch_and_caller_asserted_success_are_absent(
    tmp_path: Path,
) -> None:
    lifecycle, sessions, _runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    ceremony = await sessions.begin_authentication(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        login_url="https://example.org/login",
    )
    foreign = principal().model_copy(update={"principal_id": "principal-b"})

    with pytest.raises(ConflictError):
        await sessions.authentication_status(ceremony.id, foreign)

    public_methods = {
        name
        for name in dir(sessions)
        if not name.startswith("_") and callable(getattr(sessions, name))
    }
    assert "complete_authentication" not in public_methods
    assert "submit_credential" not in public_methods


async def assert_authentication_cancellation_is_scoped_idempotent_and_closes_runtime(
    tmp_path: Path,
) -> None:
    lifecycle, sessions, runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    ceremony = await sessions.begin_authentication(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        login_url="https://example.org/login",
    )

    cancelled = await sessions.cancel_authentication(ceremony.id, principal())
    replay = await sessions.cancel_authentication(ceremony.id, principal())

    assert cancelled.status is BrowserAuthenticationStatus.CANCELLED
    assert replay == cancelled
    assert runtimes[0].closed is True


async def test_authentication_cancellation_is_scoped_idempotent_and_closes_runtime(
    tmp_path: Path,
) -> None:
    await assert_authentication_cancellation_is_scoped_idempotent_and_closes_runtime(tmp_path)


async def test_expired_authentication_is_retained_for_bounded_idempotency(
    tmp_path: Path,
) -> None:
    lifecycle, sessions, _runtimes, times = services(tmp_path)
    await provision(lifecycle)
    ceremony = await sessions.begin_authentication(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        login_url="https://example.org/login",
    )
    times[0] = ceremony.expires_at + timedelta(seconds=1)

    expired = await sessions.authentication_status(ceremony.id, principal())
    replay = await sessions.cancel_authentication(ceremony.id, principal())

    assert expired.status is BrowserAuthenticationStatus.EXPIRED
    assert replay == expired

    times[0] += timedelta(minutes=5, seconds=1)
    with pytest.raises(ConflictError):
        await sessions.authentication_status(ceremony.id, principal())


@pytest.mark.parametrize(
    ("launch_error", "expected_reason", "expected_retryable"),
    [
        (
            BrowserProviderError("tool.browser.url_disallowed", retryable=False),
            "tool.browser.url_disallowed",
            False,
        ),
        (
            RuntimeError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://example.org/"),
            "tool.browser.provider_unavailable",
            True,
        ),
    ],
)
async def test_authentication_launch_navigation_failure_is_stable_and_discards_runtime(
    tmp_path: Path,
    launch_error: Exception,
    expected_reason: str,
    expected_retryable: bool,
) -> None:
    """A refused redirect or a lost browser leaves no raw text and no dead ceremony."""

    lifecycle, sessions, runtimes, _times = services(tmp_path, first_navigate_error=launch_error)
    await provision(lifecycle)

    with pytest.raises(BrowserProviderError) as raised:
        await sessions.begin_authentication(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            login_url="https://example.org/login",
        )
    retried = await sessions.begin_authentication(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        login_url="https://example.org/login",
    )

    assert raised.value.reason_code == expected_reason
    assert raised.value.retryable is expected_retryable
    assert "ERR_TUNNEL" not in str(raised.value)
    assert runtimes[0].closed is True
    assert retried.launch_url is not None
    assert len(runtimes) == 2
    assert runtimes[1].closed is False


# --- ADR-0128: device sign-in handoff --------------------------------------

SESSION_SENTINEL = "device-session-sentinel-value"


def device_handoff(**overrides: object) -> DeviceSessionHandoff:
    body: dict[str, object] = {
        "confirmed_url": "https://example.org/learn#unit",
        "cookies": [
            {
                "name": "session",
                "value": SESSION_SENTINEL,
                "domain": ".example.org",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "tracker",
                "value": "foreign",
                "domain": ".example.net",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "None",
            },
        ],
        "origins": [
            {"origin": "https://example.org", "localStorage": [{"name": "k", "value": "v"}]}
        ],
    }
    body.update(overrides)
    return DeviceSessionHandoff.model_validate(body)


async def begin_device(sessions: HostedProfileSessionService) -> tuple[UUID, str]:
    view = await sessions.begin_authentication(
        PROFILE_ID,
        principal(),
        PROVIDER_REF,
        login_url="https://example.org/",
        mode=BrowserAuthenticationMode.DEVICE,
    )
    assert view.launch_url is not None
    return view.id, view.launch_url.split("#capability=", 1)[1]


async def sealed_material(sessions: HostedProfileSessionService) -> bytes:
    store = sessions._store  # noqa: SLF001 - the contract inspects what was sealed
    metadata = await store.find_by_profile(PROFILE_ID)
    assert metadata is not None
    return await store.load(metadata.identity())


def record_writes(sessions: HostedProfileSessionService) -> list[bytes]:
    writes: list[bytes] = []
    store = sessions._store  # noqa: SLF001 - the contract records sealing
    original = store.write

    async def write(identity: ProfileMaterialIdentity, material: bytes) -> ProfileMaterialMetadata:
        writes.append(material)
        return await original(identity, material)

    store.write = write  # type: ignore[method-assign]
    return writes


async def verification_started(scenario: HandoffScenario, task: asyncio.Task[None]) -> None:
    """Wait until a handoff's verification loads begin, or its task ends first."""

    started = asyncio.ensure_future(scenario.started.wait())
    await asyncio.wait({started, task}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
    started.cancel()
    assert scenario.started.is_set(), "the handoff never began verifying"


async def ceremony_status(
    sessions: HostedProfileSessionService, ceremony_id: UUID
) -> BrowserAuthenticationStatus:
    return (await sessions.authentication_status(ceremony_id, principal())).status


async def assert_device_handoff_is_single_use_scope_filtered_and_service_decided(
    tmp_path: Path,
) -> None:
    """Gate 9 (ADR-0128): one handoff, filtered to the profile, decided by the service."""

    lifecycle, sessions, runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    ceremony_id, capability = await begin_device(sessions)
    assert runtimes == []

    await sessions.accept_device_session(ceremony_id, capability, device_handoff())

    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.READY
    assert await sealed_material(sessions) == b'{"format_version":1,"sealed":true}'
    with_session, without_session = runtimes
    seeded = json.loads(with_session.initial_material or b"{}")
    assert [cookie["domain"] for cookie in seeded["storage_state"]["cookies"]] == [".example.org"]
    assert without_session.initial_material == NO_SESSION
    assert (
        with_session.evidence_urls == without_session.evidence_urls == ["https://example.org/learn"]
    )
    assert with_session.closed and without_session.closed
    with pytest.raises(CeremonyCapabilityRejected):
        await sessions.accept_device_session(ceremony_id, capability, device_handoff())
    assert len(runtimes) == 2

    _lifecycle, unconfirmed, _runtimes, _times = services(
        tmp_path / "unconfirmed", scenario=HandoffScenario(without_session=None)
    )
    await provision(_lifecycle)
    other_id, other_capability = await begin_device(unconfirmed)
    with pytest.raises(DeviceSessionRejected) as rejected:
        await unconfirmed.accept_device_session(other_id, other_capability, device_handoff())
    assert rejected.value.code == "session_unconfirmed"


async def test_device_handoff_is_single_use_scope_filtered_and_service_decided(
    tmp_path: Path,
) -> None:
    await assert_device_handoff_is_single_use_scope_filtered_and_service_decided(tmp_path)


async def test_concurrent_device_handoffs_consume_the_capability_once(tmp_path: Path) -> None:
    scenario = HandoffScenario(gate=asyncio.Event())
    lifecycle, sessions, runtimes, _times = services(tmp_path, scenario=scenario)
    await provision(lifecycle)
    ceremony_id, capability = await begin_device(sessions)

    first = asyncio.create_task(
        sessions.accept_device_session(ceremony_id, capability, device_handoff())
    )
    await verification_started(scenario, first)
    with pytest.raises(CeremonyCapabilityRejected):
        await sessions.accept_device_session(ceremony_id, capability, device_handoff())
    assert scenario.gate is not None
    scenario.gate.set()
    await first

    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.READY
    assert len(runtimes) == 2


@pytest.mark.parametrize(
    ("with_session", "without_session"),
    [
        pytest.param(None, SIGNED_OUT, id="signed-out-page-shows-a-challenge"),
        pytest.param(
            None,
            BrowserPageEvidence(on_allowed_origin=True, path="/", challenge_visible=False),
            id="signed-out-page-moves",
        ),
        pytest.param(
            None,
            BrowserPageEvidence(on_allowed_origin=False, path="/", challenge_visible=False),
            id="signed-out-page-leaves-the-origins",
        ),
        pytest.param(
            BrowserPageEvidence(
                on_allowed_origin=True, path="/learn/unit-1", challenge_visible=False
            ),
            SIGNED_OUT,
            id="descendant-path-stays",
        ),
        pytest.param(
            BrowserPageEvidence(on_allowed_origin=True, path="/learn/", challenge_visible=False),
            SIGNED_OUT,
            id="trailing-slash-stays",
        ),
    ],
)
async def test_a_session_the_site_tells_apart_is_ready(
    tmp_path: Path,
    with_session: BrowserPageEvidence | None,
    without_session: BrowserPageEvidence,
) -> None:
    scenario = HandoffScenario(with_session=with_session, without_session=without_session)
    lifecycle, sessions, _runtimes, _times = services(tmp_path, scenario=scenario)
    await provision(lifecycle)
    ceremony_id, capability = await begin_device(sessions)

    await sessions.accept_device_session(ceremony_id, capability, device_handoff())

    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.READY


@pytest.mark.parametrize(
    ("with_session", "without_session", "code"),
    [
        pytest.param(SIGNED_OUT, SIGNED_OUT, "session_signed_out", id="challenge"),
        pytest.param(
            BrowserPageEvidence(on_allowed_origin=True, path="/home", challenge_visible=False),
            SIGNED_OUT,
            "session_signed_out",
            id="moved",
        ),
        pytest.param(
            BrowserPageEvidence(on_allowed_origin=True, path="/learner", challenge_visible=False),
            SIGNED_OUT,
            "session_signed_out",
            id="sibling-path-is-not-a-descendant",
        ),
        pytest.param(
            BrowserPageEvidence(on_allowed_origin=False, path="/learn", challenge_visible=False),
            SIGNED_OUT,
            "session_signed_out",
            id="off-origin",
        ),
        pytest.param(None, None, "session_unconfirmed", id="same-without-a-session"),
    ],
)
async def test_a_session_the_site_does_not_confirm_is_cancelled_and_never_sealed(
    tmp_path: Path,
    with_session: BrowserPageEvidence | None,
    without_session: BrowserPageEvidence | None,
    code: str,
) -> None:
    scenario = HandoffScenario(with_session=with_session, without_session=without_session)
    lifecycle, sessions, runtimes, _times = services(tmp_path, scenario=scenario)
    await provision(lifecycle)
    before = await sealed_material(sessions)
    writes = record_writes(sessions)
    ceremony_id, capability = await begin_device(sessions)

    with pytest.raises(DeviceSessionRejected) as rejected:
        await sessions.accept_device_session(ceremony_id, capability, device_handoff())

    assert rejected.value.code == code
    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.CANCELLED
    assert writes == []
    assert await sealed_material(sessions) == before
    assert len(runtimes) == 2 and all(runtime.closed for runtime in runtimes)


async def test_a_handoff_with_nothing_in_scope_is_empty(tmp_path: Path) -> None:
    lifecycle, sessions, runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    ceremony_id, capability = await begin_device(sessions)
    foreign_only = device_handoff(
        cookies=[
            {
                "name": "tracker",
                "value": "foreign",
                "domain": ".example.net",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        origins=[{"origin": "https://example.net", "localStorage": [{"name": "k", "value": "v"}]}],
    )

    with pytest.raises(DeviceSessionRejected) as rejected:
        await sessions.accept_device_session(ceremony_id, capability, foreign_only)

    assert rejected.value.code == "session_empty"
    assert runtimes == []
    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.CANCELLED


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(HandoffScenario(start_error=RuntimeError("private start")), id="start"),
        pytest.param(HandoffScenario(with_session="hang"), id="evidence-hangs"),
        pytest.param(
            HandoffScenario(
                without_session=BrowserProviderError(
                    "tool.browser.provider_unavailable", retryable=True
                )
            ),
            id="signed-out-load-fails",
        ),
        pytest.param(
            HandoffScenario(storage_error=ValueError("browser profile material exceeds its bound")),
            id="state-over-2-mib",
        ),
    ],
)
async def test_a_verification_that_cannot_finish_is_unavailable_and_writes_nothing(
    tmp_path: Path,
    scenario: HandoffScenario,
) -> None:
    lifecycle, sessions, runtimes, _times = services(
        tmp_path, scenario=scenario, verification_seconds=0.1
    )
    await provision(lifecycle)
    before = await sealed_material(sessions)
    writes = record_writes(sessions)
    ceremony_id, capability = await begin_device(sessions)

    with pytest.raises(BrowserProviderError) as raised:
        await sessions.accept_device_session(ceremony_id, capability, device_handoff())

    assert raised.value.reason_code == "tool.browser.provider_unavailable"
    assert "private" not in str(raised.value)
    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.CANCELLED
    assert writes == []
    assert await sealed_material(sessions) == before
    assert runtimes and all(runtime.closed for runtime in runtimes)


async def test_revocation_during_verification_wins_and_nothing_is_written(tmp_path: Path) -> None:
    scenario = HandoffScenario(gate=asyncio.Event())
    lifecycle, sessions, runtimes, _times = services(tmp_path, scenario=scenario)
    await provision(lifecycle)
    writes = record_writes(sessions)
    ceremony_id, capability = await begin_device(sessions)
    verifying = asyncio.create_task(
        sessions.accept_device_session(ceremony_id, capability, device_handoff())
    )
    await verification_started(scenario, verifying)

    await lifecycle.revoke(PROFILE_ID, principal(), PROVIDER_REF)
    assert scenario.gate is not None
    scenario.gate.set()

    with pytest.raises(BrowserProviderError) as raised:
        await verifying
    assert raised.value.reason_code == "tool.browser.profile_unavailable"
    assert writes == []
    assert all(runtime.closed for runtime in runtimes)


async def test_cancel_during_verification_wins_and_nothing_is_written(tmp_path: Path) -> None:
    scenario = HandoffScenario(gate=asyncio.Event())
    lifecycle, sessions, _runtimes, _times = services(tmp_path, scenario=scenario)
    await provision(lifecycle)
    writes = record_writes(sessions)
    ceremony_id, capability = await begin_device(sessions)
    verifying = asyncio.create_task(
        sessions.accept_device_session(ceremony_id, capability, device_handoff())
    )
    await verification_started(scenario, verifying)

    cancelled = await sessions.cancel_authentication(ceremony_id, principal())
    assert scenario.gate is not None
    scenario.gate.set()

    assert cancelled.status is BrowserAuthenticationStatus.CANCELLED
    with pytest.raises(CeremonyCapabilityRejected):
        await verifying
    assert writes == []
    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.CANCELLED


async def test_a_verifying_ceremony_is_never_expired_by_time_alone(tmp_path: Path) -> None:
    """Its own budget ends a verification; lazy expiry skips a spent capability."""

    scenario = HandoffScenario(gate=asyncio.Event())
    lifecycle, sessions, _runtimes, times = services(tmp_path, scenario=scenario)
    await provision(lifecycle)
    ceremony_id, capability = await begin_device(sessions)
    verifying = asyncio.create_task(
        sessions.accept_device_session(ceremony_id, capability, device_handoff())
    )
    await verification_started(scenario, verifying)
    times[0] = NOW + timedelta(minutes=6)

    during = await ceremony_status(sessions, ceremony_id)
    assert scenario.gate is not None
    scenario.gate.set()
    await verifying

    assert during is BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED
    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.READY


async def test_open_or_verifying_device_ceremonies_exclude_leases_and_begins(
    tmp_path: Path,
) -> None:
    scenario = HandoffScenario(gate=asyncio.Event())
    lifecycle, sessions, _runtimes, _times = services(tmp_path, scenario=scenario)
    await provision(lifecycle)
    ceremony_id, capability = await begin_device(sessions)

    async def acquire() -> BrowserLease:
        return await sessions.acquire(
            PROFILE_ID,
            principal(),
            PROVIDER_REF,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=NOW + timedelta(minutes=10),
        )

    with pytest.raises(ConflictError):
        await acquire()
    verifying = asyncio.create_task(
        sessions.accept_device_session(ceremony_id, capability, device_handoff())
    )
    await verification_started(scenario, verifying)
    with pytest.raises(ConflictError):
        await acquire()
    with pytest.raises(ConflictError):
        await begin_device(sessions)
    assert scenario.gate is not None
    scenario.gate.set()
    await verifying

    await acquire()
    with pytest.raises(ConflictError):
        await begin_device(sessions)


async def test_an_off_origin_confirmed_page_is_invalid_and_spends_nothing(tmp_path: Path) -> None:
    lifecycle, sessions, runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    ceremony_id, capability = await begin_device(sessions)

    with pytest.raises(DeviceHandoffInvalid):
        await sessions.accept_device_session(
            ceremony_id, capability, device_handoff(confirmed_url="https://example.net/learn")
        )
    assert runtimes == []
    await sessions.accept_device_session(ceremony_id, capability, device_handoff())

    assert await ceremony_status(sessions, ceremony_id) is BrowserAuthenticationStatus.READY


async def test_surface_capabilities_are_bound_to_their_mode_and_operation(tmp_path: Path) -> None:
    lifecycle, sessions, _runtimes, times = services(tmp_path)
    await provision(lifecycle)
    device_id, device_capability = await begin_device(sessions)

    assert await sessions.authenticate_surface(device_id, device_capability, "handoff")
    assert not await sessions.authenticate_surface(device_id, device_capability, "frame")
    assert not await sessions.authenticate_surface(device_id, device_capability, "events")
    assert not await sessions.authenticate_surface(device_id, "x" * 43, "handoff")
    await sessions.cancel_authentication(device_id, principal())
    assert not await sessions.authenticate_surface(device_id, device_capability, "handoff")

    remote = await sessions.begin_authentication(
        PROFILE_ID, principal(), PROVIDER_REF, login_url="https://example.org/login"
    )
    assert remote.launch_url is not None
    remote_capability = remote.launch_url.split("#capability=", 1)[1]
    assert await sessions.authenticate_surface(remote.id, remote_capability, "frame")
    assert await sessions.authenticate_surface(remote.id, remote_capability, "events")
    assert not await sessions.authenticate_surface(remote.id, remote_capability, "handoff")
    await sessions.cancel_authentication(remote.id, principal())

    later_id, later_capability = await begin_device(sessions)
    times[0] = NOW + timedelta(minutes=5)
    assert not await sessions.authenticate_surface(later_id, later_capability, "handoff")


async def test_a_spent_device_capability_no_longer_authenticates(tmp_path: Path) -> None:
    scenario = HandoffScenario(gate=asyncio.Event())
    lifecycle, sessions, _runtimes, _times = services(tmp_path, scenario=scenario)
    await provision(lifecycle)
    ceremony_id, capability = await begin_device(sessions)
    verifying = asyncio.create_task(
        sessions.accept_device_session(ceremony_id, capability, device_handoff())
    )
    await verification_started(scenario, verifying)

    during = await sessions.authenticate_surface(ceremony_id, capability, "handoff")
    assert scenario.gate is not None
    scenario.gate.set()
    await verifying

    assert during is False


async def test_terminal_ceremonies_are_capped_at_1024(tmp_path: Path) -> None:
    lifecycle, sessions, _runtimes, _times = services(tmp_path)
    await provision(lifecycle)
    first_id, _capability = await begin_device(sessions)
    await sessions.cancel_authentication(first_id, principal())
    last_id = first_id
    for _ in range(1024):
        last_id, _capability = await begin_device(sessions)
        await sessions.cancel_authentication(last_id, principal())

    assert await ceremony_status(sessions, last_id) is BrowserAuthenticationStatus.CANCELLED
    with pytest.raises(ConflictError):
        await ceremony_status(sessions, first_id)
