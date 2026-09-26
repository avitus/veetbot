"""Public browser-management routes are scoped and secret-free."""

from __future__ import annotations

import builtins
import json
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
from agent_core.adapters.browser.profiles import InMemoryBrowserProfileControlPlane
from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.api import create_app
from agent_core.application.browser_management import (
    BrowserProfileManagementService,
    BrowserUnitOfWorkFactory,
)
from agent_core.application.errors import SessionMetadataValidationError
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserActionKind,
    BrowserAuthenticationMode,
    BrowserAuthenticationStatus,
    BrowserAuthenticationView,
    BrowserGrantView,
    BrowserProfileStatus,
    BrowserProfileView,
)
from agent_core.domain.sessions import SessionStatus
from agent_core.domain.views import Page, SessionView
from tests.contract.support import NOW, memory_uow_factory, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000d7")
AUTHENTICATION_ID = UUID("00000000-0000-0000-0000-0000000000d8")
GRANT_ID = UUID("00000000-0000-0000-0000-0000000000d9")


class Profiles:
    def __init__(self) -> None:
        self.begun_modes: list[BrowserAuthenticationMode] = []

    async def create(
        self,
        owner: Principal,
        origins: tuple[str, ...],
        idempotency_key: str | None = None,
    ) -> BrowserProfileView:
        del owner, idempotency_key
        return BrowserProfileView(
            id=PROFILE_ID,
            allowed_origins=origins,
            status=BrowserProfileStatus.AUTHENTICATION_REQUIRED,
            generation=1,
            created_at=NOW,
            updated_at=NOW,
        )

    async def list(
        self,
        owner: Principal,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[BrowserProfileView]:
        del limit, cursor
        return Page(
            items=[await self.create(owner, ("https://example.org",))],
            next_cursor="next-profile",
        )

    async def get(self, owner: Principal, profile_id: UUID) -> BrowserProfileView:
        assert profile_id == PROFILE_ID
        return (await self.list(owner)).items[0]

    async def revoke(self, owner: Principal, profile_id: UUID) -> BrowserProfileView:
        return (await self.get(owner, profile_id)).model_copy(
            update={"status": BrowserProfileStatus.REVOKED, "generation": 2}
        )

    async def delete(self, owner: Principal, profile_id: UUID) -> None:
        del owner, profile_id

    async def begin_authentication(
        self,
        owner: Principal,
        profile_id: UUID,
        *,
        login_url: str,
        mode: BrowserAuthenticationMode = BrowserAuthenticationMode.REMOTE,
    ) -> BrowserAuthenticationView:
        del owner, login_url
        self.begun_modes.append(mode)
        return self._ceremony(profile_id, mode)

    @staticmethod
    def _ceremony(profile_id: UUID, mode: BrowserAuthenticationMode) -> BrowserAuthenticationView:
        suffix = "/handoff" if mode is BrowserAuthenticationMode.DEVICE else ""
        return BrowserAuthenticationView(
            id=AUTHENTICATION_ID,
            profile_id=profile_id,
            status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
            expires_at=NOW + timedelta(minutes=5),
            launch_url=(
                f"https://login.example.test/authentication/{AUTHENTICATION_ID}{suffix}"
                "#capability=one-time-capability"
            ),
        )

    async def list_authentications(
        self,
        owner: Principal,
        profile_id: UUID,
    ) -> builtins.list[BrowserAuthenticationView]:
        del owner
        result = self._ceremony(profile_id, BrowserAuthenticationMode.REMOTE)
        return [result.model_copy(update={"launch_url": None})]

    async def authentication_status(
        self,
        owner: Principal,
        authentication_id: UUID,
    ) -> BrowserAuthenticationView:
        assert authentication_id == AUTHENTICATION_ID
        return (await self.list_authentications(owner, PROFILE_ID))[0]

    async def cancel_authentication(
        self,
        owner: Principal,
        authentication_id: UUID,
    ) -> BrowserAuthenticationView:
        return (await self.authentication_status(owner, authentication_id)).model_copy(
            update={"status": BrowserAuthenticationStatus.CANCELLED}
        )


class Grants:
    async def create(self, owner: Principal, **values: Any) -> BrowserGrantView:
        del owner
        return BrowserGrantView(
            id=GRANT_ID,
            profile_id=values["profile_id"],
            profile_generation=2,
            agent_version="agent-v1",
            policy_version="policy-v1",
            allowed_origins=values["allowed_origins"],
            action_kinds=values["action_kinds"],
            element_roles=values["element_roles"],
            element_names=values["element_names"],
            purpose=values["purpose"],
            starts_at=values["starts_at"],
            expires_at=values["expires_at"],
            approved_by=principal().principal_id,
            revoked_at=None,
            created_at=NOW,
            updated_at=NOW,
        )

    async def list(
        self,
        owner: Principal,
        *,
        profile_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[BrowserGrantView]:
        del owner, profile_id, limit, cursor
        return Page(items=[], next_cursor="next-grant")

    async def get(self, owner: Principal, grant_id: UUID) -> BrowserGrantView:
        assert grant_id == GRANT_ID
        raise AssertionError("unused")

    async def revoke(self, owner: Principal, grant_id: UUID) -> BrowserGrantView:
        del owner, grant_id
        raise AssertionError("unused")

    async def delete(self, owner: Principal, grant_id: UUID) -> None:
        del owner, grant_id


class Sessions:
    def __init__(self) -> None:
        self.browser_profile_id: UUID | None = None

    async def create(
        self,
        owner: Principal,
        agent_id: str,
        metadata: dict[str, object],
        browser_profile_id: UUID | None = None,
    ) -> SessionView:
        del owner
        self.browser_profile_id = browser_profile_id
        return SessionView(
            id=UUID("00000000-0000-0000-0000-0000000000da"),
            status=SessionStatus.ACTIVE,
            agent_id=agent_id,
            agent_version="1",
            title=None,
            metadata={**metadata, "browser_profile_id": str(browser_profile_id)},
            created_at=NOW,
            updated_at=NOW,
            active_run_id=None,
            last_run_id=None,
        )


class MetadataRejectingSessions(Sessions):
    async def create(
        self,
        owner: Principal,
        agent_id: str,
        metadata: dict[str, object],
        browser_profile_id: UUID | None = None,
    ) -> SessionView:
        del owner, agent_id, metadata, browser_profile_id
        raise SessionMetadataValidationError("session metadata is invalid")


class UnexpectedValueErrorSessions(Sessions):
    async def create(
        self,
        owner: Principal,
        agent_id: str,
        metadata: dict[str, object],
        browser_profile_id: UUID | None = None,
    ) -> SessionView:
        del owner, agent_id, metadata, browser_profile_id
        raise ValueError("unexpected downstream value error")


def settings() -> Settings:
    return Settings(
        people_enabled=False,  # This fixture supplies only the browser public services.
        database_url="postgresql+asyncpg://unused/agent",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials=MappingProxyType({}),
        interpolation=MappingProxyType({"OPENAI_MODEL": ""}),
    )


async def test_public_profile_authentication_and_grant_creation_are_secret_free() -> None:
    services = SimpleNamespace(
        sessions=None,
        runs=None,
        approvals=None,
        artifacts=None,
        browser_profiles=Profiles(),
        browser_grants=Grants(),
    )
    owner = principal().model_copy(
        update={
            "scopes": {
                "browser.profile.read",
                "browser.profile.write",
                "browser.grant.read",
                "browser.grant.write",
            }
        }
    )
    app = create_app(services, settings(), owner, lambda: str(PROFILE_ID), _ready)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        created = await client.post(
            "/v1/browser-profiles",
            headers={"Idempotency-Key": "create-profile-1"},
            json={"allowed_origins": ["https://example.org"]},
        )
        ceremony = await client.post(
            f"/v1/browser-profiles/{PROFILE_ID}/authentication-ceremonies",
            json={"login_url": "https://example.org/login"},
        )
        status = await client.get(f"/v1/browser-authentication-ceremonies/{AUTHENTICATION_ID}")
        grant = await client.post(
            "/v1/browser-grants",
            headers={"Idempotency-Key": "grant-1"},
            json={
                "profile_id": str(PROFILE_ID),
                "allowed_origins": ["https://example.org"],
                "action_kinds": [BrowserActionKind.CLICK],
                "element_roles": ["button"],
                "element_names": ["Continue"],
                "purpose": "language-practice",
                "starts_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(days=7)).isoformat(),
            },
        )

    assert created.status_code == 201
    assert ceremony.status_code == 201
    assert "capability=" in ceremony.json()["launch_url"]
    assert status.status_code == 200
    assert status.json()["launch_url"] is None
    assert grant.status_code == 201
    serialized = created.text + status.text + grant.text
    assert "provider_ref" not in serialized
    assert "storage_state" not in serialized
    assert "encryption_key_version" not in serialized


async def test_public_session_creation_passes_only_the_opaque_browser_profile_binding() -> None:
    sessions = Sessions()
    services = SimpleNamespace(
        sessions=sessions,
        runs=None,
        approvals=None,
        artifacts=None,
        browser_profiles=Profiles(),
        browser_grants=Grants(),
    )
    owner = principal().model_copy(update={"scopes": {"session.write"}})
    app = create_app(services, settings(), owner, lambda: str(PROFILE_ID), _ready)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": "general",
                "metadata": {},
                "browser_profile_id": str(PROFILE_ID),
            },
        )

    assert response.status_code == 201
    assert sessions.browser_profile_id == PROFILE_ID
    assert response.json()["metadata"] == {"browser_profile_id": str(PROFILE_ID)}
    assert "password" not in response.text


async def test_session_metadata_service_validation_is_a_malformed_request() -> None:
    services = SimpleNamespace(
        sessions=MetadataRejectingSessions(),
        runs=None,
        approvals=None,
        artifacts=None,
        browser_profiles=Profiles(),
        browser_grants=Grants(),
    )
    owner = principal().model_copy(update={"scopes": {"session.write"}})
    app = create_app(services, settings(), owner, lambda: str(PROFILE_ID), _ready)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": "general",
                "metadata": {"browser_profile_id": str(PROFILE_ID)},
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_request"


async def test_unexpected_session_value_error_remains_an_internal_error() -> None:
    services = SimpleNamespace(
        sessions=UnexpectedValueErrorSessions(),
        runs=None,
        approvals=None,
        artifacts=None,
        browser_profiles=Profiles(),
        browser_grants=Grants(),
    )
    owner = principal().model_copy(update={"scopes": {"session.write"}})
    app = create_app(services, settings(), owner, lambda: str(PROFILE_ID), _ready)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            "/v1/sessions",
            json={"agent_id": "general", "metadata": {}},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


async def test_browser_write_requests_reject_malformed_origins_and_grant_windows() -> None:
    services = SimpleNamespace(
        sessions=None,
        runs=None,
        approvals=None,
        artifacts=None,
        browser_profiles=Profiles(),
        browser_grants=Grants(),
    )
    owner = principal().model_copy(
        update={"scopes": {"browser.profile.write", "browser.grant.write"}}
    )
    app = create_app(services, settings(), owner, lambda: str(PROFILE_ID), _ready)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        malformed_origin = await client.post(
            "/v1/browser-profiles",
            headers={"Idempotency-Key": "invalid-origin"},
            json={"allowed_origins": ["https://example.org/path"]},
        )
        inverted_window = await client.post(
            "/v1/browser-grants",
            headers={"Idempotency-Key": "invalid-window"},
            json={
                "profile_id": str(PROFILE_ID),
                "allowed_origins": ["https://example.org"],
                "action_kinds": [BrowserActionKind.CLICK],
                "starts_at": NOW.isoformat(),
                "expires_at": NOW.isoformat(),
            },
        )

    assert malformed_origin.status_code == 400
    assert inverted_window.status_code == 400


@pytest.mark.parametrize("mode", [None, "device"])
@pytest.mark.parametrize(
    ("login_url", "request_owner", "expected_status", "expected_code"),
    [
        ("https://www.duolingo.com/?isLoggingIn=true", "owner", 400, "malformed_request"),
        ("https://duolingo.com.other.example/login", "owner", 400, "malformed_request"),
        ("Duolingo.com", "owner", 400, "malformed_request"),
        ("http://duolingo.com/login", "owner", 400, "malformed_request"),
        ("https://user:" + "private-value@duolingo.com/login", "owner", 400, "malformed_request"),
        ("https://127.0.0.1/login", "owner", 400, "malformed_request"),
        ("https://www.duolingo.com/login", "other_principal", 404, "not_found"),
        ("https://www.duolingo.com/login", "other_tenant", 404, "not_found"),
        ("https://www.duolingo.com/login", "without_scope", 403, "authorization_error"),
    ],
)
async def test_browser_login_validation_precedes_provider_dispatch_and_allows_retry(
    login_url: str,
    request_owner: str,
    expected_status: int,
    expected_code: str,
    mode: str | None,
) -> None:
    mode_field = {} if mode is None else {"mode": mode}
    clock, uow_factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"browser.profile.write"}})
    requesting = owner
    if request_owner == "other_principal":
        requesting = owner.model_copy(update={"principal_id": "another-principal"})
    elif request_owner == "other_tenant":
        requesting = owner.model_copy(update={"tenant_id": "another-tenant"})
    elif request_owner == "without_scope":
        requesting = owner.model_copy(update={"scopes": {"browser.profile.read"}})
    provider_requests: list[httpx.Request] = []

    def provider_response(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        if json.loads(request.content)["login_url"] != "https://duolingo.com/?isLoggingIn=true":
            # The isolated provider already rejects the reported www mismatch.
            return httpx.Response(409, json={"error": {"code": "tool.browser.url_disallowed"}})
        return httpx.Response(
            201,
            json={
                "id": str(AUTHENTICATION_ID),
                "profile_id": str(PROFILE_ID),
                "status": "authentication_required",
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                "launch_url": "https://login.example.test/authentication#capability=one-time",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_response)) as provider:
        profiles = BrowserProfileManagementService(
            uow_factory=cast(BrowserUnitOfWorkFactory, uow_factory),
            lifecycle=InMemoryBrowserProfileControlPlane(),
            authentications=HostedBrowserSessionControlPlane(
                base_url="https://login.example.test",
                credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
                client=provider,
            ),
            clock=clock,
            ids=SequenceIdFactory([PROFILE_ID]),
        )
        created = await profiles.create(owner, ("https://Duolingo.com/",))
        services = SimpleNamespace(browser_profiles=profiles)
        app = create_app(services, settings(), requesting, lambda: str(PROFILE_ID), _ready)
        path = f"/v1/browser-profiles/{PROFILE_ID}/authentication-ceremonies"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1",
        ) as client:
            rejected = await client.post(path, json={"login_url": login_url, **mode_field})

        assert rejected.status_code == expected_status
        assert "Check the Website origin and Login page fields" not in rejected.text
        error = rejected.json()["error"]
        assert error["code"] == expected_code
        assert error["details"] == {}
        assert error["request_id"] == str(PROFILE_ID)
        assert login_url not in rejected.text
        assert "private-value" not in rejected.text
        if expected_status == 400:
            assert "HTTPS" in error["message"] or "exact website origins" in error["message"]
        assert provider_requests == []
        async with uow_factory() as uow:
            unchanged = await uow.browser_profiles.get(PROFILE_ID, owner)
            assert unchanged.generation == created.generation
            assert unchanged.status == created.status
            assert await uow.browser_authentications.list(owner, profile_id=PROFILE_ID) == []

        app = create_app(services, settings(), owner, lambda: str(PROFILE_ID), _ready)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            retried = await client.post(
                path,
                json={"login_url": "https://duolingo.com/?isLoggingIn=true", **mode_field},
            )
        assert retried.status_code == 201
        assert retried.json()["launch_url"] is not None
        assert len(provider_requests) == 1
        assert json.loads(provider_requests[0].content).get("mode") == mode


async def test_browser_write_routes_reject_principals_without_exact_scopes() -> None:
    services = SimpleNamespace(
        sessions=None,
        runs=None,
        approvals=None,
        artifacts=None,
        browser_profiles=Profiles(),
        browser_grants=Grants(),
    )
    insufficient = principal().model_copy(update={"scopes": {"browser.profile.read"}})
    app = create_app(services, settings(), insufficient, lambda: str(PROFILE_ID), _ready)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        profile = await client.post(
            "/v1/browser-profiles",
            headers={"Idempotency-Key": "scope-profile"},
            json={"allowed_origins": ["https://example.org"]},
        )
        grant = await client.post(
            "/v1/browser-grants",
            headers={"Idempotency-Key": "scope-grant"},
            json={
                "profile_id": str(PROFILE_ID),
                "allowed_origins": ["https://example.org"],
                "action_kinds": [BrowserActionKind.CLICK],
                "starts_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(days=1)).isoformat(),
            },
        )

    assert profile.status_code == 403
    assert grant.status_code == 403


async def test_browser_profile_and_grant_collections_use_stable_page_shapes() -> None:
    services = SimpleNamespace(
        sessions=None,
        runs=None,
        approvals=None,
        artifacts=None,
        browser_profiles=Profiles(),
        browser_grants=Grants(),
    )
    owner = principal().model_copy(
        update={"scopes": {"browser.profile.read", "browser.grant.read"}}
    )
    app = create_app(services, settings(), owner, lambda: str(PROFILE_ID), _ready)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    ) as client:
        profiles = await client.get("/v1/browser-profiles", params={"limit": 1})
        grants = await client.get("/v1/browser-grants", params={"limit": 1})

    assert profiles.json()["next_cursor"] == "next-profile"
    assert profiles.json()["items"][0]["id"] == str(PROFILE_ID)
    assert grants.json() == {"items": [], "next_cursor": "next-grant"}


async def _ready() -> bool:
    return True


async def test_browser_login_redirect_outside_allowed_origins_is_a_malformed_request() -> None:
    """A refused document redirect gets guidance without the removed CDN settings."""

    clock, uow_factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"browser.profile.write"}})
    provider_requests: list[httpx.Request] = []
    redirect_blocked = [True]

    def provider_response(request: httpx.Request) -> httpx.Response:
        """Return the requested fake provider outcome at the public browser API boundary."""
        provider_requests.append(request)
        if redirect_blocked[0]:
            # The isolated runtime refused the site's redirect to an unlisted origin.
            return httpx.Response(409, json={"error": {"code": "tool.browser.url_disallowed"}})
        return httpx.Response(
            201,
            json={
                "id": str(AUTHENTICATION_ID),
                "profile_id": str(PROFILE_ID),
                "status": "authentication_required",
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                "launch_url": "https://login.example.test/authentication#capability=one-time",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider_response)) as provider:
        profiles = BrowserProfileManagementService(
            uow_factory=cast(BrowserUnitOfWorkFactory, uow_factory),
            lifecycle=InMemoryBrowserProfileControlPlane(),
            authentications=HostedBrowserSessionControlPlane(
                base_url="https://login.example.test",
                credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
                client=provider,
            ),
            clock=clock,
            ids=SequenceIdFactory([PROFILE_ID]),
        )
        created = await profiles.create(owner, ("https://duolingo.com",))
        services = SimpleNamespace(browser_profiles=profiles)
        app = create_app(services, settings(), owner, lambda: str(PROFILE_ID), _ready)
        path = f"/v1/browser-profiles/{PROFILE_ID}/authentication-ceremonies"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://127.0.0.1",
        ) as client:
            rejected = await client.post(path, json={"login_url": "https://duolingo.com/"})
            redirect_blocked[0] = False
            retried = await client.post(path, json={"login_url": "https://duolingo.com/"})

    assert rejected.status_code == 400
    error = rejected.json()["error"]
    assert error["code"] == "malformed_request"
    assert "redirect" in error["message"]
    assert "www" in error["message"]
    assert "Advanced settings" not in error["message"]
    assert error["details"] == {}
    assert error["request_id"] == str(PROFILE_ID)
    assert "duolingo" not in rejected.text
    assert retried.status_code == 201
    assert retried.json()["launch_url"] is not None
    assert len(provider_requests) == 2
    async with uow_factory() as uow:
        profile = await uow.browser_profiles.get(PROFILE_ID, owner)
        # The refused begin changed nothing; the retried begin advanced the
        # generation once, as every sign-in attempt does (ADR-0128 decision 10).
        assert profile.generation == created.generation + 1
        records = await uow.browser_authentications.list(owner, profile_id=PROFILE_ID)
        assert [record.id for record in records] == [AUTHENTICATION_ID]


def _profile_services(profiles: Profiles) -> SimpleNamespace:
    return SimpleNamespace(
        sessions=None,
        runs=None,
        approvals=None,
        artifacts=None,
        browser_profiles=profiles,
        browser_grants=Grants(),
    )


async def test_device_ceremony_begin_passes_mode_and_returns_launch_once() -> None:
    """ADR-0128 section 2.1: device mode rides on the existing begin route."""

    profiles = Profiles()
    owner = principal().model_copy(
        update={"scopes": {"browser.profile.read", "browser.profile.write"}}
    )
    app = create_app(
        _profile_services(profiles), settings(), owner, lambda: str(PROFILE_ID), _ready
    )
    path = f"/v1/browser-profiles/{PROFILE_ID}/authentication-ceremonies"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        device = await client.post(
            path, json={"login_url": "https://example.org/", "mode": "device"}
        )
        remote = await client.post(path, json={"login_url": "https://example.org/"})
        bogus = await client.post(path, json={"login_url": "https://example.org/", "mode": "bogus"})
        status = await client.get(f"/v1/browser-authentication-ceremonies/{AUTHENTICATION_ID}")
        listed = await client.get(path)

    assert device.status_code == 201, device.text
    assert (
        device.json()["launch_url"]
        .split("#")[0]
        .endswith(f"/authentication/{AUTHENTICATION_ID}/handoff")
    )
    assert remote.status_code == 201, remote.text
    assert "/handoff" not in remote.json()["launch_url"]
    assert bogus.status_code == 400
    assert bogus.json()["error"]["code"] == "malformed_request"
    assert "bogus" not in bogus.text
    assert profiles.begun_modes == [
        BrowserAuthenticationMode.DEVICE,
        BrowserAuthenticationMode.REMOTE,
    ]
    assert status.json()["launch_url"] is None
    assert [item["launch_url"] for item in listed.json()] == [None]


@pytest.mark.parametrize("mode", [None, "remote", "device"])
async def test_begin_response_is_private_no_store(mode: str | None) -> None:
    """ADR-0128 (S4): the begin body carries a capability, so no cache keeps it."""

    owner = principal().model_copy(update={"scopes": {"browser.profile.write"}})
    app = create_app(
        _profile_services(Profiles()), settings(), owner, lambda: str(PROFILE_ID), _ready
    )
    body: dict[str, str] = {"login_url": "https://example.org/"}
    if mode is not None:
        body["mode"] = mode
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            f"/v1/browser-profiles/{PROFILE_ID}/authentication-ceremonies", json=body
        )

    assert response.status_code == 201, response.text
    assert response.headers.get("cache-control") == "private, no-store"


class DeviceCeremonyService:
    """A stateful isolated service behind MockTransport (ADR-0128 section 2.4).

    Each begin opens a new ceremony. ``reject_handoffs`` models the owner's
    client posting a handoff the service refused: the service already
    records the ceremony ``cancelled``, which orchestration learns only
    through status or cancel.
    """

    def __init__(self) -> None:
        self.statuses: dict[UUID, str] = {}
        self.begun: list[dict[str, object]] = []
        self.reject_handoffs = False

    def _view(self, ceremony_id: UUID, *, launch: bool) -> dict[str, object]:
        view: dict[str, object] = {
            "id": str(ceremony_id),
            "profile_id": str(PROFILE_ID),
            "status": self.statuses[ceremony_id],
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        }
        if launch:
            view["launch_url"] = (
                f"https://browser.example.test/authentication/{ceremony_id}/handoff"
                "#capability=one-time"
            )
        return view

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith(":begin"):
            self.begun.append(body)
            ceremony_id = UUID(int=0xD100 + len(self.begun))
            self.statuses[ceremony_id] = (
                "cancelled" if self.reject_handoffs else "authentication_required"
            )
            view = self._view(ceremony_id, launch=True)
            view["status"] = "authentication_required"
            return httpx.Response(201, json=view)
        ceremony_id = UUID(body["ceremony_id"])
        if request.url.path.endswith(":cancel") and self.statuses[ceremony_id] in {
            "authentication_required",
            "needs_user",
        }:
            self.statuses[ceremony_id] = "cancelled"
        return httpx.Response(200, json=self._view(ceremony_id, launch=False))


async def _device_begin_client(
    service: DeviceCeremonyService,
) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    clock, uow_factory = await memory_uow_factory()
    owner = principal().model_copy(
        update={"scopes": {"browser.profile.read", "browser.profile.write"}}
    )
    provider = httpx.AsyncClient(transport=httpx.MockTransport(service))
    profiles = BrowserProfileManagementService(
        uow_factory=cast(BrowserUnitOfWorkFactory, uow_factory),
        lifecycle=InMemoryBrowserProfileControlPlane(),
        authentications=HostedBrowserSessionControlPlane(
            base_url="https://browser.example.test",
            credentials=MappingCredentialResolver({"browser_profile_control_plane": "test"}),
            client=provider,
        ),
        clock=clock,
        ids=SequenceIdFactory([PROFILE_ID]),
    )
    await profiles.create(owner, ("https://www.example.org",))
    app = create_app(
        SimpleNamespace(browser_profiles=profiles),
        settings(),
        owner,
        lambda: str(PROFILE_ID),
        _ready,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    )
    return provider, client


DEVICE_BEGIN = {"login_url": "https://www.example.org/", "mode": "device"}
CEREMONIES = f"/v1/browser-profiles/{PROFILE_ID}/authentication-ceremonies"


async def test_device_begin_after_a_rejected_handoff_is_409_until_cancelled() -> None:
    """ADR-0128 D20: a refused handoff leaves the record open until cancel."""

    service = DeviceCeremonyService()
    service.reject_handoffs = True
    provider, client = await _device_begin_client(service)
    async with provider, client:
        first = await client.post(CEREMONIES, json=DEVICE_BEGIN)
        blocked = await client.post(CEREMONIES, json=DEVICE_BEGIN)
        cancelled = await client.post(
            f"/v1/browser-authentication-ceremonies/{first.json()['id']}/cancel"
        )
        service.reject_handoffs = False
        retried = await client.post(CEREMONIES, json=DEVICE_BEGIN)

    assert first.status_code == 201, first.text
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "conflict"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert retried.status_code == 201, retried.text
    assert retried.json()["id"] != first.json()["id"]
    assert [body.get("mode") for body in service.begun] == ["device", "device"]


async def test_device_begin_after_reading_a_cancelled_status_is_admitted() -> None:
    service = DeviceCeremonyService()
    service.reject_handoffs = True
    provider, client = await _device_begin_client(service)
    async with provider, client:
        first = await client.post(CEREMONIES, json=DEVICE_BEGIN)
        status = await client.get(f"/v1/browser-authentication-ceremonies/{first.json()['id']}")
        service.reject_handoffs = False
        retried = await client.post(CEREMONIES, json=DEVICE_BEGIN)

    assert status.json()["status"] == "cancelled"
    assert status.json()["launch_url"] is None
    assert retried.status_code == 201, retried.text


async def test_lost_device_begin_is_recovered_by_list_cancel_and_begin() -> None:
    """ADR-0128 D20 (B6): the client never saw the id, so it lists, cancels
    the newest open ceremony and begins once more."""

    service = DeviceCeremonyService()
    provider, client = await _device_begin_client(service)
    async with provider, client:
        lost = await client.post(CEREMONIES, json=DEVICE_BEGIN)
        del lost  # The response never reached the client.
        listed = await client.get(CEREMONIES)
        open_ceremonies = [
            item
            for item in listed.json()
            if item["status"] in {"authentication_required", "needs_user"}
        ]
        newest = max(open_ceremonies, key=lambda item: item["expires_at"])
        cancelled = await client.post(
            f"/v1/browser-authentication-ceremonies/{newest['id']}/cancel"
        )
        retried = await client.post(CEREMONIES, json=DEVICE_BEGIN)

    assert len(open_ceremonies) == 1
    assert all(item["launch_url"] is None for item in listed.json())
    assert cancelled.json()["status"] == "cancelled"
    assert retried.status_code == 201, retried.text
    assert "/handoff#capability=" in retried.json()["launch_url"]
