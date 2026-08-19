"""Exclusive hosted browser leases and direct authentication ceremonies."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationStatus,
    BrowserInteractiveEvent,
    BrowserObservation,
    BrowserProviderError,
)
from agent_core.domain.errors import ConflictError
from tests.contract.support import NOW, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000f4")
RUN_ID = UUID("00000000-0000-0000-0000-0000000000f5")
PROVIDER_REF = "opaque-session-reference-0000000000000001"


@dataclass
class FakeSessionRuntime:
    initial_material: bytes | None = None
    allowed_origins: tuple[str, ...] = ()
    interactive: bool = False
    sealed_material: bytes = b'{"format_version":1,"sealed":true}'
    authentication: BrowserAuthenticationStatus = BrowserAuthenticationStatus.READY
    closed: bool = False
    actions: list[BrowserAction] = field(default_factory=list)

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

    async def navigate(self, url: str) -> BrowserObservation:
        return BrowserObservation(url=url, revision="revision-1", text="safe observation")

    async def observe(self) -> BrowserObservation:
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

    async def storage_state(self) -> bytes:
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
) -> tuple[
    HostedProfileLifecycleService,
    HostedProfileSessionService,
    list[FakeSessionRuntime],
    list[datetime],
]:
    keyring = StaticProfileKeyring(
        {"key-v1": hashlib.sha256(b"synthetic-session-key").digest()},
        current_version="key-v1",
    )
    store = FilesystemEncryptedProfileStore(tmp_path / "profiles", keyring)
    runtimes: list[FakeSessionRuntime] = []
    times = [NOW]

    def runtime_factory(tenant_id: str) -> FakeSessionRuntime:
        assert tenant_id == principal().tenant_id
        runtime = FakeSessionRuntime()
        runtimes.append(runtime)
        return runtime

    sessions = HostedProfileSessionService(
        store,
        runtime_factory=runtime_factory,
        now=lambda: times[0],
        process_secret=b"synthetic-process-secret-with-32-bytes",
        ceremony_base_url="https://browser-login.example.test",
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
