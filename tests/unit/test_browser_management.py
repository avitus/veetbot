"""Public browser profile, authentication, and standing-grant services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import TracebackType
from typing import Self, cast
from uuid import UUID

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
    BrowserActionKind,
    BrowserAuthenticationStatus,
    BrowserAuthenticationView,
    BrowserProfileStatus,
)
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


def owner(*scopes: str) -> Principal:
    return principal().model_copy(update={"scopes": set(scopes)})


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
