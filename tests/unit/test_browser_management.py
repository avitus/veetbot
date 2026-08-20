"""Public browser profile, authentication, and standing-grant services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Self, cast
from uuid import UUID

import pytest

from agent_core.adapters.browser.authentications import (
    InMemoryBrowserAuthenticationRepository,
)
from agent_core.adapters.browser.grants import InMemoryBrowserGrantRepository
from agent_core.adapters.browser.profiles import (
    InMemoryBrowserProfileControlPlane,
    InMemoryBrowserProfileRepository,
)
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.application.browser_management import (
    BrowserGrantManagementService,
    BrowserProfileManagementService,
    BrowserUnitOfWorkFactory,
)
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    ALLOWED_BROWSER_PROFILE_TRANSITIONS,
    BrowserActionKind,
    BrowserAuthenticationStatus,
    BrowserAuthenticationView,
    BrowserProfileStatus,
    BrowserProviderError,
)
from agent_core.domain.errors import AuthorizationError, ConflictError
from tests.contract.support import NOW, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000c7")
CEREMONY_ID = UUID("00000000-0000-0000-0000-0000000000c8")
GRANT_ID = UUID("00000000-0000-0000-0000-0000000000c9")


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.browser_profiles = InMemoryBrowserProfileRepository()
        self.browser_authentications = InMemoryBrowserAuthenticationRepository()
        self.browser_grants = InMemoryBrowserGrantRepository()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


class FakeUnitOfWorkFactory:
    def __init__(self) -> None:
        self.uow = FakeUnitOfWork()

    def __call__(self) -> FakeUnitOfWork:
        return self.uow

    def is_open(self) -> bool:
        return False


@dataclass
class FakeAuthenticationControlPlane:
    status: BrowserAuthenticationStatus = BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED

    async def begin_authentication(
        self,
        profile_id: UUID,
        owner: Principal,
        provider_ref: str,
        *,
        login_url: str,
    ) -> BrowserAuthenticationView:
        del owner, provider_ref, login_url
        return BrowserAuthenticationView(
            id=CEREMONY_ID,
            profile_id=profile_id,
            status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
            expires_at=NOW + timedelta(minutes=5),
            launch_url=(
                f"https://login.example.test/authentication/{CEREMONY_ID}"
                "#capability=one-time-secret-capability"
            ),
        )

    async def authentication_status(
        self,
        ceremony_id: UUID,
        owner: Principal,
    ) -> BrowserAuthenticationView:
        del owner
        return BrowserAuthenticationView(
            id=ceremony_id,
            profile_id=PROFILE_ID,
            status=self.status,
            expires_at=NOW + timedelta(minutes=5),
        )

    async def cancel_authentication(
        self,
        ceremony_id: UUID,
        owner: Principal,
    ) -> BrowserAuthenticationView:
        self.status = BrowserAuthenticationStatus.CANCELLED
        return await self.authentication_status(ceremony_id, owner)


class BlockingAuthenticationControlPlane(FakeAuthenticationControlPlane):
    def __init__(self) -> None:
        super().__init__()
        self.begin_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def begin_authentication(
        self,
        profile_id: UUID,
        owner: Principal,
        provider_ref: str,
        *,
        login_url: str,
    ) -> BrowserAuthenticationView:
        self.begin_calls += 1
        self.started.set()
        await self.release.wait()
        return await super().begin_authentication(
            profile_id,
            owner,
            provider_ref,
            login_url=login_url,
        )


def owner(*scopes: str) -> Principal:
    return principal().model_copy(update={"scopes": set(scopes)})


def test_browser_profile_transition_table_is_total() -> None:
    assert set(ALLOWED_BROWSER_PROFILE_TRANSITIONS) == set(BrowserProfileStatus)


async def test_browser_creation_requires_exact_write_scopes() -> None:
    uow = FakeUnitOfWorkFactory()
    profiles = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=FakeAuthenticationControlPlane(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    grants = BrowserGrantManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([GRANT_ID]),
        agent_version="agent-v1",
        policy_version="policy-v1",
    )

    with pytest.raises(AuthorizationError):
        await profiles.create(owner("browser.profile.read"), ("https://example.org",))
    with pytest.raises(AuthorizationError):
        await grants.create(
            owner("browser.grant.read"),
            profile_id=PROFILE_ID,
            allowed_origins=("https://example.org",),
            action_kinds=(BrowserActionKind.CLICK,),
            element_roles=(),
            element_names=(),
            purpose=None,
            starts_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )


async def test_profile_authentication_is_durable_secret_free_and_runtime_decided() -> None:
    uow = FakeUnitOfWorkFactory()
    authentication = FakeAuthenticationControlPlane()
    profiles = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=authentication,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    principal_with_scopes = owner("browser.profile.read", "browser.profile.write")

    profile = await profiles.create(principal_with_scopes, ("https://example.org",))
    ceremony = await profiles.begin_authentication(
        principal_with_scopes,
        PROFILE_ID,
        login_url="https://example.org/login",
    )
    persisted = await uow.uow.browser_authentications.get(CEREMONY_ID, principal_with_scopes)
    authentication.status = BrowserAuthenticationStatus.NEEDS_USER
    needs_user = await profiles.authentication_status(principal_with_scopes, CEREMONY_ID)
    authentication.status = BrowserAuthenticationStatus.READY
    ready = await profiles.authentication_status(principal_with_scopes, CEREMONY_ID)
    updated_profile = await uow.uow.browser_profiles.get(PROFILE_ID, principal_with_scopes)

    assert profile.status is BrowserProfileStatus.AUTHENTICATION_REQUIRED
    assert ceremony.launch_url is not None and "capability=" in ceremony.launch_url
    assert "capability" not in persisted.model_dump_json()
    assert needs_user.status is BrowserAuthenticationStatus.NEEDS_USER
    assert needs_user.launch_url is None
    assert ready.status is BrowserAuthenticationStatus.READY
    assert updated_profile.status is BrowserProfileStatus.READY


async def test_standing_grant_creation_pins_profile_agent_policy_and_approver() -> None:
    uow = FakeUnitOfWorkFactory()
    authentication = FakeAuthenticationControlPlane(status=BrowserAuthenticationStatus.READY)
    profiles = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=authentication,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    principal_with_scopes = owner(
        "browser.profile.read",
        "browser.profile.write",
        "browser.grant.read",
        "browser.grant.write",
    )
    await profiles.create(principal_with_scopes, ("https://example.org",))
    await profiles.begin_authentication(
        principal_with_scopes,
        PROFILE_ID,
        login_url="https://example.org/login",
    )
    await profiles.authentication_status(principal_with_scopes, CEREMONY_ID)
    grants = BrowserGrantManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([GRANT_ID]),
        agent_version="agent-v1",
        policy_version="policy-v1",
    )

    created = await grants.create(
        principal_with_scopes,
        profile_id=PROFILE_ID,
        allowed_origins=("https://example.org",),
        action_kinds=(BrowserActionKind.CLICK,),
        element_roles=("button",),
        element_names=("Continue",),
        purpose="language-practice",
        starts_at=NOW,
        expires_at=NOW + timedelta(days=7),
    )

    assert created.id == GRANT_ID
    assert created.agent_version == "agent-v1"
    assert created.policy_version == "policy-v1"
    assert created.approved_by == principal_with_scopes.principal_id
    assert "tenant_id" not in created.model_dump()
    assert "principal_id" not in created.model_dump()


async def test_profile_revoke_invalidates_provider_before_committing_metadata() -> None:
    operations: list[str] = []

    class OrderedRepository(InMemoryBrowserProfileRepository):
        async def transition(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            operations.append("metadata:revoked")
            return await super().transition(*args, **kwargs)  # type: ignore[arg-type]

    class OrderedLifecycle(InMemoryBrowserProfileControlPlane):
        async def revoke(self, *args: object, **kwargs: object) -> None:
            operations.append("provider:revoked")
            await super().revoke(*args, **kwargs)  # type: ignore[arg-type]

    uow = FakeUnitOfWorkFactory()
    uow.uow.browser_profiles = OrderedRepository()
    service = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=OrderedLifecycle(),
        authentications=FakeAuthenticationControlPlane(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    subject = owner("browser.profile.write")
    created = await service.create(subject, ("https://example.org",))
    operations.clear()

    await service.revoke(subject, created.id)

    assert operations == ["provider:revoked", "metadata:revoked"]


async def test_profile_creation_preserves_original_and_compensation_failures() -> None:
    class FailingRepository(InMemoryBrowserProfileRepository):
        async def transition(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise RuntimeError("compensation failed")

    class FailingLifecycle(InMemoryBrowserProfileControlPlane):
        async def provision(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise RuntimeError("provision failed")

    uow = FakeUnitOfWorkFactory()
    uow.uow.browser_profiles = FailingRepository()
    service = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=FailingLifecycle(),
        authentications=FakeAuthenticationControlPlane(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )

    with pytest.raises(ExceptionGroup) as raised:
        await service.create(owner("browser.profile.write"), ("https://example.org",))

    assert {str(error) for error in raised.value.exceptions} == {
        "provision failed",
        "compensation failed",
    }


async def test_authentication_creation_preserves_persistence_and_cancel_failures() -> None:
    class FailingAuthenticationRepository(InMemoryBrowserAuthenticationRepository):
        async def create(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise RuntimeError("authentication persistence failed")

    class FailingAuthenticationControlPlane(FakeAuthenticationControlPlane):
        async def cancel_authentication(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            del args, kwargs
            raise RuntimeError("authentication cancel failed")

    uow = FakeUnitOfWorkFactory()
    uow.uow.browser_authentications = FailingAuthenticationRepository()
    service = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=FailingAuthenticationControlPlane(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    subject = owner("browser.profile.write")
    await service.create(subject, ("https://example.org",))

    with pytest.raises(ExceptionGroup) as raised:
        await service.begin_authentication(
            subject,
            PROFILE_ID,
            login_url="https://example.org/login",
        )

    assert {str(error) for error in raised.value.exceptions} == {
        "authentication persistence failed",
        "authentication cancel failed",
    }


async def test_revoked_profile_ignores_late_ready_authentication_status() -> None:
    uow = FakeUnitOfWorkFactory()
    authentication = FakeAuthenticationControlPlane()
    lifecycle = InMemoryBrowserProfileControlPlane()
    service = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=lifecycle,
        authentications=authentication,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    subject = owner("browser.profile.read", "browser.profile.write")
    await service.create(subject, ("https://example.org",))
    await service.begin_authentication(
        subject,
        PROFILE_ID,
        login_url="https://example.org/login",
    )
    await service.revoke(subject, PROFILE_ID)
    authentication.status = BrowserAuthenticationStatus.READY

    status = await service.authentication_status(subject, CEREMONY_ID)
    stored = await uow.uow.browser_profiles.get(PROFILE_ID, subject)

    assert status.status is BrowserAuthenticationStatus.READY
    assert stored.status is BrowserProfileStatus.REVOKED


async def test_active_authentication_ceremony_is_not_replayed_without_launch_capability() -> None:
    uow = FakeUnitOfWorkFactory()
    service = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=FakeAuthenticationControlPlane(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    subject = owner("browser.profile.write")
    await service.create(subject, ("https://example.org",))
    await service.begin_authentication(
        subject,
        PROFILE_ID,
        login_url="https://example.org/login",
    )

    with pytest.raises(ConflictError):
        await service.begin_authentication(
            subject,
            PROFILE_ID,
            login_url="https://example.org/login",
        )


async def test_concurrent_authentication_requests_admit_only_one_ceremony() -> None:
    uow = FakeUnitOfWorkFactory()
    authentication = BlockingAuthenticationControlPlane()
    service = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=authentication,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    subject = owner("browser.profile.write")
    await service.create(subject, ("https://example.org",))

    first = asyncio.create_task(
        service.begin_authentication(
            subject,
            PROFILE_ID,
            login_url="https://example.org/login",
        )
    )
    await authentication.started.wait()
    second = asyncio.create_task(
        service.begin_authentication(
            subject,
            PROFILE_ID,
            login_url="https://example.org/login",
        )
    )
    await asyncio.sleep(0)
    calls_before_release = authentication.begin_calls
    authentication.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert calls_before_release == 1
    assert authentication.begin_calls == 1
    assert sum(isinstance(result, BrowserAuthenticationView) for result in results) == 1
    assert sum(isinstance(result, ConflictError) for result in results) == 1


async def test_authentication_provider_launch_has_total_deadline() -> None:
    class StalledAuthenticationControlPlane(FakeAuthenticationControlPlane):
        async def begin_authentication(
            self,
            profile_id: UUID,
            owner: Principal,
            provider_ref: str,
            *,
            login_url: str,
        ) -> BrowserAuthenticationView:
            del profile_id, owner, provider_ref, login_url
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    uow = FakeUnitOfWorkFactory()
    service = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=StalledAuthenticationControlPlane(),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
        authentication_timeout_seconds=0.01,
    )
    subject = owner("browser.profile.write")
    await service.create(subject, ("https://example.org",))

    with pytest.raises(BrowserProviderError) as raised:
        await service.begin_authentication(
            subject,
            PROFILE_ID,
            login_url="https://example.org/login",
        )

    assert raised.value.reason_code == "tool.browser.provider_unavailable"
    assert raised.value.retryable is True


async def test_authentication_admission_lock_wait_is_bounded() -> None:
    uow = FakeUnitOfWorkFactory()
    authentication = BlockingAuthenticationControlPlane()
    service = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=authentication,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory([PROFILE_ID]),
        authentication_lock_timeout_seconds=0.01,
    )
    subject = owner("browser.profile.write")
    await service.create(subject, ("https://example.org",))
    first = asyncio.create_task(
        service.begin_authentication(
            subject,
            PROFILE_ID,
            login_url="https://example.org/login",
        )
    )
    await authentication.started.wait()

    with pytest.raises(ConflictError):
        await service.begin_authentication(
            subject,
            PROFILE_ID,
            login_url="https://example.org/login",
        )

    authentication.release.set()
    await asyncio.wait_for(first, timeout=1)
    assert authentication.begin_calls == 1
