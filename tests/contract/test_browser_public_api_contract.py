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
    ) -> BrowserAuthenticationView:
        del owner, login_url
        return BrowserAuthenticationView(
            id=AUTHENTICATION_ID,
            profile_id=profile_id,
            status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
            expires_at=NOW + timedelta(minutes=5),
            launch_url=(
                f"https://login.example.test/authentication/{AUTHENTICATION_ID}"
                "#capability=one-time-capability"
            ),
        )

    async def list_authentications(
        self,
        owner: Principal,
        profile_id: UUID,
    ) -> builtins.list[BrowserAuthenticationView]:
        result = await self.begin_authentication(owner, profile_id, login_url="unused")
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
) -> None:
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
            rejected = await client.post(path, json={"login_url": login_url})

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
                path, json={"login_url": "https://duolingo.com/?isLoggingIn=true"}
            )
        assert retried.status_code == 201
        assert retried.json()["launch_url"] is not None
        assert len(provider_requests) == 1


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
