"""Boundary contract for the isolated profile lifecycle HTTP server."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from agent_core.adapters.browser.hosted_profiles import HostedBrowserProfileControlPlane
from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.browser_control_plane.api import create_profile_service_app
from agent_core.browser_control_plane.filesystem import FilesystemEncryptedProfileStore
from agent_core.browser_control_plane.handoff import DeviceSessionHandoff
from agent_core.browser_control_plane.ports import StaticProfileKeyring
from agent_core.browser_control_plane.service import HostedProfileLifecycleService
from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.domain.browser import (
    BrowserAction,
    BrowserActionKind,
    BrowserAuthenticationStatus,
    BrowserInteractiveEvent,
    BrowserObservation,
    BrowserPageEvidence,
    BrowserProviderError,
)
from agent_core.domain.credentials import SecretValue
from tests.contract.support import NOW, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000f3")
OPAQUE_AUTH_VALUE = "synthetic-profile-service-auth-value"
PROVIDER_REF = "opaque-http-reference-000000000000000001"
RUN_ID = UUID("00000000-0000-0000-0000-0000000000f8")


NO_SESSION = b'{"format_version":1}'


class FakeRuntime:
    def __init__(
        self,
        events: list[BrowserInteractiveEvent] | None = None,
        *,
        evidence_failure: Exception | None = None,
    ) -> None:
        self.origins: tuple[str, ...] = ()
        self.closed = False
        self.events = events if events is not None else []
        self.material: bytes | None = None
        self.evidence_failure = evidence_failure

    async def start(
        self,
        material: bytes,
        allowed_origins: tuple[str, ...],
        *,
        interactive: bool,
    ) -> None:
        del interactive
        self.material = material
        self.origins = allowed_origins

    async def navigate(self, url: str) -> BrowserObservation:
        return BrowserObservation(url=url, revision="revision-1")

    async def observe(self) -> BrowserObservation:
        return BrowserObservation(url=self.origins[0], revision="revision-1")

    async def act(self, action: BrowserAction) -> BrowserObservation:
        del action
        return BrowserObservation(url=self.origins[0], revision="revision-2")

    async def load_page_evidence(self, url: str) -> BrowserPageEvidence:
        """A public home page, and members' pages that send a visitor to sign in."""
        if self.evidence_failure is not None:
            raise self.evidence_failure
        if self.material == NO_SESSION and urlsplit(url).path not in {"", "/"}:
            return BrowserPageEvidence(
                on_allowed_origin=True, path="/log-in", challenge_visible=True
            )
        return BrowserPageEvidence(
            on_allowed_origin=True, path=urlsplit(url).path or "/", challenge_visible=False
        )

    async def storage_state(self) -> bytes:
        return b'{"format_version":1,"wire":true}'

    async def authentication_status(self) -> BrowserAuthenticationStatus:
        return BrowserAuthenticationStatus.NEEDS_USER

    async def close(self) -> None:
        self.closed = True

    async def interactive_frame(self) -> bytes:
        return b"synthetic-png-frame"

    async def interactive_event(self, event: BrowserInteractiveEvent) -> None:
        self.events.append(event)


def lifecycle(root: Path) -> HostedProfileLifecycleService:
    keyring = StaticProfileKeyring(
        {"key-v1": hashlib.sha256(b"synthetic-http-key").digest()},
        current_version="key-v1",
    )
    return HostedProfileLifecycleService(
        FilesystemEncryptedProfileStore(root, keyring),
        reference_factory=lambda: PROVIDER_REF,
    )


def app(root: Path, *, readiness: bool = True) -> FastAPI:
    return create_profile_service_app(
        lifecycle(root),
        SecretValue(OPAQUE_AUTH_VALUE),
        readiness=lambda: readiness,
    )


def full_app(
    root: Path,
    *,
    events: list[BrowserInteractiveEvent] | None = None,
    runtimes: list[FakeRuntime] | None = None,
    device_sign_in_enabled: bool = True,
    times: list[datetime] | None = None,
    evidence_failure: Exception | None = None,
    services: list[HostedProfileSessionService] | None = None,
) -> FastAPI:
    store = FilesystemEncryptedProfileStore(
        root,
        StaticProfileKeyring(
            {"key-v1": hashlib.sha256(b"synthetic-http-key").digest()},
            current_version="key-v1",
        ),
    )
    created = runtimes if runtimes is not None else []

    def runtime_factory(tenant_id: str) -> FakeRuntime:
        del tenant_id
        runtime = FakeRuntime(events, evidence_failure=evidence_failure)
        created.append(runtime)
        return runtime

    clock = times if times is not None else [NOW]
    sessions = HostedProfileSessionService(
        store,
        runtime_factory=runtime_factory,
        now=lambda: clock[0],
        process_secret=b"synthetic-http-process-secret-32-bytes",
        ceremony_base_url="https://login.example.test",
        device_sign_in_enabled=device_sign_in_enabled,
    )
    if services is not None:
        services.append(sessions)
    lifecycle_service = HostedProfileLifecycleService(
        store,
        reference_factory=lambda: PROVIDER_REF,
        invalidate_profile=sessions.invalidate_profile,
    )
    return create_profile_service_app(
        lifecycle_service,
        SecretValue(OPAQUE_AUTH_VALUE),
        sessions=sessions,
    )


async def test_profile_service_is_wire_compatible_with_hosted_client(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=app(tmp_path / "profiles"))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://profiles.internal.example",
    ) as http:
        client = HostedBrowserProfileControlPlane(
            base_url="https://profiles.internal.example",
            credentials=MappingCredentialResolver(
                {"browser_profile_control_plane": OPAQUE_AUTH_VALUE}
            ),
            client=http,
        )
        provisioned = await client.provision(
            PROFILE_ID,
            principal(),
            ("https://example.org",),
        )
        await client.revoke(PROFILE_ID, principal(), provisioned.provider_ref)
        await client.delete(PROFILE_ID, principal(), provisioned.provider_ref)

    assert provisioned.provider_ref == PROVIDER_REF
    assert list((tmp_path / "profiles").glob("*.profile")) == []


async def test_profile_service_authenticates_before_buffering_body(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=app(tmp_path / "profiles"))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as client:
        response = await client.post(
            "/v1/browser-profiles:provision",
            headers={
                "Authorization": "Bearer wrong-value",
                "Content-Type": "application/json",
                "Content-Length": "999999",
            },
            content=b"not parsed",
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "headers,content,status",
    [
        ({"Authorization": f"Bearer {OPAQUE_AUTH_VALUE}"}, b"{}", 415),
        (
            {
                "Authorization": f"Bearer {OPAQUE_AUTH_VALUE}",
                "Content-Type": "application/json",
            },
            b"x" * 65_537,
            413,
        ),
        (
            {
                "Authorization": f"Bearer {OPAQUE_AUTH_VALUE}",
                "Content-Type": "application/json",
                "Idempotency-Key": "wrong",
            },
            b"{}",
            400,
        ),
    ],
)
async def test_profile_service_rejects_media_size_and_idempotency_before_core(
    tmp_path: Path,
    headers: dict[str, str],
    content: bytes,
    status: int,
) -> None:
    transport = httpx.ASGITransport(app=app(tmp_path / "profiles"))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as client:
        response = await client.post(
            f"/v1/browser-profiles/{PROFILE_ID}:revoke",
            headers=headers,
            content=content,
        )

    assert response.status_code == status
    assert PROVIDER_REF not in response.text


async def test_profile_service_rejects_extra_fields_and_path_body_mismatch(
    tmp_path: Path,
) -> None:
    transport = httpx.ASGITransport(app=app(tmp_path / "profiles"))
    headers = {
        "Authorization": f"Bearer {OPAQUE_AUTH_VALUE}",
        "Content-Type": "application/json",
        "Idempotency-Key": f"browser-profile:{PROFILE_ID}:revoke",
    }
    body = {
        "profile_id": "00000000-0000-0000-0000-0000000000ff",
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "provider_ref": PROVIDER_REF,
        "unexpected": "value",
    }
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as client:
        response = await client.post(
            f"/v1/browser-profiles/{PROFILE_ID}:revoke",
            headers=headers,
            json=body,
        )

    assert response.status_code == 400
    assert "unexpected" not in response.text


async def test_profile_service_rejects_naive_lease_deadlines_at_the_boundary(
    tmp_path: Path,
) -> None:
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles"))
    payload = {
        "profile_id": str(PROFILE_ID),
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "provider_ref": PROVIDER_REF,
        "run_id": str(RUN_ID),
        "attempt_number": 1,
        "deadline_at": "2026-08-20T12:00:00",
    }
    headers = {
        "Authorization": f"Bearer {OPAQUE_AUTH_VALUE}",
        "Idempotency-Key": f"browser-session:{PROFILE_ID}:{RUN_ID}:1:acquire",
    }

    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as client:
        response = await client.post(
            "/v1/browser-sessions:acquire",
            headers=headers,
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("deadline", "idempotency_key"),
    [
        ("2026-08-20T12:00:00", "valid"),
        ("2026-08-20T12:00:00+00:00", "browser-session:someone-else:renew"),
    ],
)
async def test_profile_service_renew_route_rejects_naive_deadlines_and_foreign_intent(
    tmp_path: Path,
    deadline: str,
    idempotency_key: str,
) -> None:
    lease_ref = "r" * 43
    digest = hashlib.sha256(lease_ref.encode()).hexdigest()[:24]
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles"))
    headers = {
        "Authorization": f"Bearer {OPAQUE_AUTH_VALUE}",
        "Idempotency-Key": (
            f"browser-session:{digest}:renew" if idempotency_key == "valid" else idempotency_key
        ),
    }

    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as client:
        response = await client.post(
            "/v1/browser-sessions:renew",
            headers=headers,
            json={"lease_ref": lease_ref, "deadline_at": deadline},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_profile_service_health_and_surface_are_minimal(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=app(tmp_path / "profiles", readiness=False))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        openapi = await client.get("/openapi.json")
        exported = await client.get(f"/v1/browser-profiles/{PROFILE_ID}")

    assert live.status_code == 200 and live.json() == {"status": "ok"}
    assert ready.status_code == 503 and ready.json() == {"status": "not_ready"}
    assert openapi.status_code == 404
    assert exported.status_code == 404


async def test_profile_service_suppresses_unexpected_diagnostics(tmp_path: Path) -> None:
    service = lifecycle(tmp_path / "profiles")

    async def fail(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("sensitive-provider-diagnostic")

    service.provision = fail  # type: ignore[method-assign,assignment]
    transport = httpx.ASGITransport(
        app=create_profile_service_app(service, SecretValue(OPAQUE_AUTH_VALUE)),
        raise_app_exceptions=False,
    )
    headers = {
        "Authorization": f"Bearer {OPAQUE_AUTH_VALUE}",
        "Content-Type": "application/json",
        "Idempotency-Key": f"browser-profile:{PROFILE_ID}:provision",
    }
    body = {
        "profile_id": str(PROFILE_ID),
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "allowed_origins": ["https://example.org"],
    }
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as client:
        response = await client.post("/v1/browser-profiles:provision", headers=headers, json=body)

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "service unavailable"}
    }
    assert "sensitive-provider-diagnostic" not in response.text


async def test_profile_service_data_plane_and_authentication_are_wire_compatible(
    tmp_path: Path,
) -> None:
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles"))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://profiles.internal.example",
    ) as http:
        lifecycle_client = HostedBrowserProfileControlPlane(
            base_url="https://profiles.internal.example",
            credentials=MappingCredentialResolver(
                {"browser_profile_control_plane": OPAQUE_AUTH_VALUE}
            ),
            client=http,
        )
        sessions = HostedBrowserSessionControlPlane(
            base_url="https://profiles.internal.example",
            credentials=MappingCredentialResolver(
                {"browser_profile_control_plane": OPAQUE_AUTH_VALUE}
            ),
            client=http,
        )
        provisioned = await lifecycle_client.provision(
            PROFILE_ID,
            principal(),
            ("https://example.org",),
        )
        lease = await sessions.acquire(
            PROFILE_ID,
            principal(),
            provisioned.provider_ref,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=NOW + timedelta(minutes=5),
        )
        observation = await sessions.navigate(
            lease.lease_ref,
            "https://example.org/lesson",
        )
        acted = await sessions.act(
            lease.lease_ref,
            BrowserAction(
                kind=BrowserActionKind.CLICK,
                expected_revision="revision-1",
                ref="revision-1:0",
            ),
            sequence=1,
        )
        renewed = await sessions.renew(lease.lease_ref, deadline_at=NOW + timedelta(minutes=10))
        reattached = await sessions.acquire(
            PROFILE_ID,
            principal(),
            provisioned.provider_ref,
            run_id=RUN_ID,
            attempt_number=1,
            deadline_at=NOW + timedelta(minutes=5),
        )
        await sessions.close(lease.lease_ref)
        ceremony = await sessions.begin_authentication(
            PROFILE_ID,
            principal(),
            provisioned.provider_ref,
            login_url="https://example.org/login",
        )
        status = await sessions.authentication_status(ceremony.id, principal())
        cancelled = await sessions.cancel_authentication(ceremony.id, principal())

    assert observation.url == "https://example.org/lesson"
    assert acted.revision == "revision-2"
    assert renewed.lease_ref == lease.lease_ref
    assert renewed.expires_at == NOW + timedelta(minutes=10)
    assert (reattached.lease_ref, reattached.sequence) == (lease.lease_ref, 1)
    assert ceremony.launch_url is not None and "#capability=" in ceremony.launch_url
    assert status.status is BrowserAuthenticationStatus.NEEDS_USER
    assert status.launch_url is None
    assert cancelled.status is BrowserAuthenticationStatus.CANCELLED


async def test_authentication_surface_binds_fragment_capability_before_interaction_body(
    tmp_path: Path,
) -> None:
    events: list[BrowserInteractiveEvent] = []
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles", events=events))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://profiles.internal.example",
    ) as http:
        lifecycle_client = HostedBrowserProfileControlPlane(
            base_url="https://profiles.internal.example",
            credentials=MappingCredentialResolver(
                {"browser_profile_control_plane": OPAQUE_AUTH_VALUE}
            ),
            client=http,
        )
        sessions = HostedBrowserSessionControlPlane(
            base_url="https://profiles.internal.example",
            credentials=MappingCredentialResolver(
                {"browser_profile_control_plane": OPAQUE_AUTH_VALUE}
            ),
            client=http,
        )
        provisioned = await lifecycle_client.provision(
            PROFILE_ID,
            principal(),
            ("https://example.org",),
        )
        ceremony = await sessions.begin_authentication(
            PROFILE_ID,
            principal(),
            provisioned.provider_ref,
            login_url="https://example.org/login",
        )
        assert ceremony.launch_url is not None
        launch = urlsplit(ceremony.launch_url)
        capability = parse_qs(launch.fragment)["capability"][0]

        surface = await http.get(launch.path)
        script = await http.get("/authentication-surface.js")
        unauthorized = await http.post(
            launch.path + "/events",
            headers={"Content-Type": "application/json", "Content-Length": "999999"},
            content=b"not parsed",
        )
        frame = await http.get(
            launch.path + "/frame",
            headers={"X-Browser-Ceremony-Capability": capability},
        )
        event = await http.post(
            launch.path + "/events",
            headers={
                "X-Browser-Ceremony-Capability": capability,
                "Content-Type": "application/json",
            },
            json={"kind": "click", "x": 100, "y": 120},
        )

    assert surface.status_code == 200
    assert surface.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in surface.headers["content-security-policy"]
    assert capability not in surface.text
    assert "How to sign in" in surface.text
    assert "Click the website field in the remote browser" in surface.text
    assert "Return to Veetbot" in surface.text
    assert "Start over" in surface.text
    assert 'autocomplete="off"' in surface.text
    assert script.status_code == 200
    assert "if(!capability)" in script.text
    assert "This secure login link is incomplete or has expired" in script.text
    assert "controls.forEach(control=>control.disabled=true)" in script.text
    assert unauthorized.status_code == 401
    assert frame.content == b"synthetic-png-frame"
    assert frame.headers["content-type"] == "image/png"
    assert event.status_code == 204
    assert events == [BrowserInteractiveEvent(kind="click", x=100, y=120)]


SERVICE_HEADERS = {
    "Authorization": f"Bearer {OPAQUE_AUTH_VALUE}",
    "Content-Type": "application/json",
}


async def _provision_over_http(http: httpx.AsyncClient) -> str:
    response = await http.post(
        "/v1/browser-profiles:provision",
        headers={**SERVICE_HEADERS, "Idempotency-Key": f"browser-profile:{PROFILE_ID}:provision"},
        json={
            "profile_id": str(PROFILE_ID),
            "tenant_id": principal().tenant_id,
            "principal_id": principal().principal_id,
            "allowed_origins": ["https://example.org"],
        },
    )
    assert response.status_code == 201
    return str(response.json()["provider_ref"])


async def _begin_over_http(
    http: httpx.AsyncClient,
    provider_ref: str,
    *,
    mode: str | None,
    login_url: str = "https://example.org/",
) -> httpx.Response:
    body: dict[str, object] = {
        "profile_id": str(PROFILE_ID),
        "tenant_id": principal().tenant_id,
        "principal_id": principal().principal_id,
        "provider_ref": provider_ref,
        "login_url": login_url,
    }
    if mode is not None:
        body["mode"] = mode
    return await http.post(
        "/v1/browser-authentications:begin",
        headers={
            **SERVICE_HEADERS,
            "Idempotency-Key": f"browser-authentication:{PROFILE_ID}:begin",
        },
        json=body,
    )


async def test_device_ceremony_begin_returns_a_handoff_capability_and_starts_no_browser(
    tmp_path: Path,
) -> None:
    """A device begin issues a handoff capability; the owner's client signs in (ADR-0128)."""

    runtimes: list[FakeRuntime] = []
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles", runtimes=runtimes))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        provider_ref = await _provision_over_http(http)
        begun = await _begin_over_http(http, provider_ref, mode="device")
        status = await http.post(
            "/v1/browser-authentications:status",
            headers=SERVICE_HEADERS,
            json={
                "ceremony_id": begun.json().get("id"),
                "tenant_id": principal().tenant_id,
                "principal_id": principal().principal_id,
            },
        )
        bogus = await _begin_over_http(http, provider_ref, mode="bogus")

    assert begun.status_code == 201
    view = begun.json()
    launch = urlsplit(view["launch_url"])
    assert (launch.scheme, launch.netloc) == ("https", "login.example.test")
    assert launch.path == f"/authentication/{view['id']}/handoff"
    capability = parse_qs(launch.fragment)["capability"][0]
    assert len(capability) == 43
    assert view["status"] == "authentication_required"
    assert runtimes == []
    assert status.status_code == 200
    assert status.json()["status"] == "authentication_required"
    assert status.json().get("launch_url") is None
    assert bogus.status_code == 400


async def test_remote_ceremony_begin_is_unchanged_without_a_mode(tmp_path: Path) -> None:
    runtimes: list[FakeRuntime] = []
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles", runtimes=runtimes))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        provider_ref = await _provision_over_http(http)
        begun = await _begin_over_http(http, provider_ref, mode=None)

    assert begun.status_code == 201
    launch = urlsplit(begun.json()["launch_url"])
    assert launch.path == f"/authentication/{begun.json()['id']}"
    assert len(runtimes) == 1


async def test_device_begin_is_refused_when_device_sign_in_is_off(tmp_path: Path) -> None:
    """The service kill switch refuses device begins and leaves remote ones alone (D13)."""

    runtimes: list[FakeRuntime] = []
    transport = httpx.ASGITransport(
        app=full_app(tmp_path / "profiles", runtimes=runtimes, device_sign_in_enabled=False)
    )
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        provider_ref = await _provision_over_http(http)
        refused = await _begin_over_http(http, provider_ref, mode="device")
        remote = await _begin_over_http(http, provider_ref, mode="remote")

    assert refused.status_code == 409
    assert refused.json() == {
        "error": {
            "code": "tool.browser.provider_unavailable",
            "message": "browser operation rejected",
        }
    }
    assert remote.status_code == 201
    assert len(runtimes) == 1


# --- ADR-0128: the device sign-in handoff at the service boundary -----------

HANDOFF_SENTINEL = "handoff-boundary-sentinel-value"


def handoff_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "confirmed_url": "https://example.org/learn",
        "cookies": [
            {
                "name": "session",
                "value": HANDOFF_SENTINEL,
                "domain": ".example.org",
                "path": "/",
                "expires": 1790000000.5,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    body.update(overrides)
    return body


async def _begin_device(http: httpx.AsyncClient) -> tuple[str, str, str]:
    """Provision the profile and begin a device ceremony: (id, handoff path, capability)."""

    provider_ref = await _provision_over_http(http)
    begun = await _begin_over_http(http, provider_ref, mode="device")
    assert begun.status_code == 201, begun.text
    launch = urlsplit(begun.json()["launch_url"])
    return begun.json()["id"], launch.path, parse_qs(launch.fragment)["capability"][0]


async def _ceremony_status(http: httpx.AsyncClient, ceremony_id: str) -> str:
    response = await http.post(
        "/v1/browser-authentications:status",
        headers=SERVICE_HEADERS,
        json={
            "ceremony_id": ceremony_id,
            "tenant_id": principal().tenant_id,
            "principal_id": principal().principal_id,
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


def _handoff_headers(capability: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if capability is not None:
        headers["X-Browser-Ceremony-Capability"] = capability
    return headers


async def test_device_handoff_seals_the_verified_session_once(tmp_path: Path) -> None:
    """ADR-0128 happy path: one handoff, verified and sealed, then the capability is spent."""

    runtimes: list[FakeRuntime] = []
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles", runtimes=runtimes))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        ceremony_id, path, capability = await _begin_device(http)
        sealed = await http.post(path, headers=_handoff_headers(capability), json=handoff_body())
        status = await _ceremony_status(http, ceremony_id)
        replay = await http.post(path, headers=_handoff_headers(capability), json=handoff_body())

    assert sealed.status_code == 200, sealed.text
    assert sealed.json() == {"status": "ready"}
    assert sealed.headers["cache-control"] == "no-store"
    assert sealed.headers["x-content-type-options"] == "nosniff"
    assert status == "ready"
    assert replay.status_code == 401
    assert replay.json() == {
        "error": {"code": "unauthorized", "message": "authentication required"}
    }
    assert len(runtimes) == 2 and all(runtime.closed for runtime in runtimes)
    for response in (sealed, replay):
        assert HANDOFF_SENTINEL not in response.text
        assert capability not in response.text


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(handoff_body(extra=True), id="extra-field"),
        pytest.param(handoff_body(confirmed_url="http://example.org/learn"), id="http"),
        pytest.param(
            handoff_body(cookies=[{**handoff_body()["cookies"][0], "value": "a;b"}]),  # type: ignore[index]
            id="bad-cookie",
        ),
        pytest.param(handoff_body(confirmed_url="https://example.net/learn"), id="off-origin"),
        pytest.param(b"not json", id="not-json"),
    ],
)
async def test_a_malformed_handoff_is_400_and_spends_nothing(
    tmp_path: Path, body: dict[str, object] | bytes
) -> None:
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles"))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        _ceremony_id, path, capability = await _begin_device(http)
        headers = _handoff_headers(capability)
        if isinstance(body, bytes):
            rejected = await http.post(path, headers=headers, content=body)
        else:
            rejected = await http.post(path, headers=headers, json=body)
        accepted = await http.post(path, headers=headers, json=handoff_body())

    assert rejected.status_code == 400
    assert rejected.json() == {
        "error": {"code": "invalid_request", "message": "request is invalid"}
    }
    assert HANDOFF_SENTINEL not in rejected.text
    assert accepted.status_code == 200


async def test_device_handoff_authenticates_before_reading_the_body(tmp_path: Path) -> None:
    """A missing, wrong, remote, expired or cancelled capability is 401 before any body."""

    times = [NOW]
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles", times=times))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        _ceremony_id, path, capability = await _begin_device(http)
        unread = {"Content-Type": "application/json", "Content-Length": "999999"}
        missing = await http.post(path, headers=unread, content=b"not parsed")
        wrong = await http.post(
            path,
            headers={**unread, "X-Browser-Ceremony-Capability": "w" * 43},
            content=b"not parsed",
        )
        frame = await http.get(
            path.removesuffix("/handoff") + "/frame",
            headers={"X-Browser-Ceremony-Capability": capability},
        )
        events = await http.post(
            path.removesuffix("/handoff") + "/events",
            headers=_handoff_headers(capability),
            json={"kind": "click", "x": 1, "y": 1},
        )
        unsupported = await http.post(
            path,
            headers={"X-Browser-Ceremony-Capability": capability, "Content-Type": "text/plain"},
            content=b"{}",
        )
        declared_too_large = await http.post(
            path,
            headers={**_handoff_headers(capability), "Content-Length": str(1024 * 1024 + 1)},
            content=b"x",
        )
        streamed_too_large = await http.post(
            path, headers=_handoff_headers(capability), content=b" " * (1024 * 1024 + 1)
        )
        times[0] = NOW + timedelta(minutes=5)
        expired = await http.post(path, headers=_handoff_headers(capability), json=handoff_body())

    assert [response.status_code for response in (missing, wrong, frame, events)] == [401] * 4
    assert unsupported.status_code == 415
    assert declared_too_large.status_code == 413
    assert streamed_too_large.status_code == 413
    assert expired.status_code == 401
    for response in (missing, wrong, frame, events, unsupported, declared_too_large, expired):
        assert response.headers["cache-control"] == "no-store"


async def test_a_remote_capability_never_authorizes_a_handoff(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles"))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        provider_ref = await _provision_over_http(http)
        begun = await _begin_over_http(http, provider_ref, mode="remote")
        launch = urlsplit(begun.json()["launch_url"])
        capability = parse_qs(launch.fragment)["capability"][0]
        refused = await http.post(
            launch.path + "/handoff", headers=_handoff_headers(capability), json=handoff_body()
        )

    assert refused.status_code == 401


async def test_after_a_rejected_handoff_the_ceremony_is_cancelled_and_a_new_one_begins(
    tmp_path: Path,
) -> None:
    """Retry means a new ceremony (ADR-0128 D4)."""

    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles"))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        ceremony_id, path, capability = await _begin_device(http)
        unconfirmed = await http.post(
            path,
            headers=_handoff_headers(capability),
            json=handoff_body(confirmed_url="https://example.org/"),
        )
        status = await _ceremony_status(http, ceremony_id)
        retried = await http.post(path, headers=_handoff_headers(capability), json=handoff_body())
        again = await _begin_over_http(http, PROVIDER_REF, mode="device")

    assert unconfirmed.status_code == 422, unconfirmed.text
    assert unconfirmed.json()["error"]["code"] == "session_unconfirmed"
    assert status == "cancelled"
    assert retried.status_code == 401
    assert again.status_code == 201


async def test_a_cancelled_device_ceremony_refuses_its_handoff(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=full_app(tmp_path / "profiles"))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        ceremony_id, path, capability = await _begin_device(http)
        cancelled = await http.post(
            "/v1/browser-authentications:cancel",
            headers={
                **SERVICE_HEADERS,
                "Idempotency-Key": f"browser-authentication:{ceremony_id}:cancel",
            },
            json={
                "ceremony_id": ceremony_id,
                "tenant_id": principal().tenant_id,
                "principal_id": principal().principal_id,
            },
        )
        refused = await http.post(path, headers=_handoff_headers(capability), json=handoff_body())

    assert cancelled.status_code == 200
    assert refused.status_code == 401


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        pytest.param(
            BrowserProviderError("tool.browser.provider_unavailable", retryable=True),
            409,
            "tool.browser.provider_unavailable",
            id="provider-unavailable",
        ),
        pytest.param(
            RuntimeError(f"unexpected {HANDOFF_SENTINEL}"),
            409,
            "tool.browser.provider_unavailable",
            id="load-crashes",
        ),
    ],
)
async def test_a_verification_failure_is_a_fixed_answer(
    tmp_path: Path, failure: Exception, status: int, code: str
) -> None:
    transport = httpx.ASGITransport(
        app=full_app(tmp_path / "profiles", evidence_failure=failure),
        raise_app_exceptions=True,
    )
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        ceremony_id, path, capability = await _begin_device(http)
        answered = await http.post(path, headers=_handoff_headers(capability), json=handoff_body())
        after = await _ceremony_status(http, ceremony_id)

    assert answered.status_code == status
    assert answered.json() == {"error": {"code": code, "message": "browser operation rejected"}}
    assert HANDOFF_SENTINEL not in answered.text
    assert after == "cancelled"


# --- ADR-0128 D18: nothing from a handoff reaches a log or escapes -----------

LOG_SENTINEL_NAME = "log-sentinel-cookie-name"
LOG_SENTINEL_VALUE = "log-sentinel-cookie-value"
LOG_SENTINEL_STORAGE = "log-sentinel-storage-item"
LOG_SENTINEL_PATH = "log-sentinel-path"
_LOG_SENTINELS = (LOG_SENTINEL_NAME, LOG_SENTINEL_VALUE, LOG_SENTINEL_STORAGE, LOG_SENTINEL_PATH)


def sentinel_handoff(**overrides: object) -> dict[str, object]:
    cookie = {
        "name": LOG_SENTINEL_NAME,
        "value": LOG_SENTINEL_VALUE,
        "domain": ".example.org",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    body: dict[str, object] = {
        "confirmed_url": f"https://example.org/{LOG_SENTINEL_PATH}",
        "cookies": [cookie, {**cookie, "domain": ".example.net"}],
        "origins": [
            {
                "origin": "https://example.org",
                "localStorage": [{"name": LOG_SENTINEL_STORAGE, "value": LOG_SENTINEL_STORAGE}],
            }
        ],
    }
    body.update(overrides)
    return body


def injected_failures() -> list[Exception]:
    """A RuntimeError with a sentinel and a chained cause, a KeyError, a ValidationError."""

    try:
        raise KeyError(LOG_SENTINEL_NAME)
    except KeyError as cause:
        chained = RuntimeError(f"verification failed for {LOG_SENTINEL_VALUE}")
        chained.__cause__ = cause
    try:
        DeviceSessionHandoff.model_validate(
            {"confirmed_url": LOG_SENTINEL_VALUE, "cookies": [], "origins": []}
        )
    except ValidationError as error:
        validation = error
    return [chained, KeyError(LOG_SENTINEL_VALUE), validation]


def _leaks(records: list[logging.LogRecord], secrets: tuple[str, ...]) -> list[str]:
    leaks: list[str] = []
    for record in records:
        rendered = " ".join(
            (
                record.getMessage(),
                repr(record.args),
                str(record.exc_text),
                repr({key: value for key, value in vars(record).items() if key != "exc_info"}),
            )
        )
        if record.exc_info is not None:
            rendered += logging.Formatter().formatException(record.exc_info)
        leaks.extend(secret for secret in secrets if secret in rendered)
    return leaks


async def _handoff_once(
    root: Path, body: dict[str, object], *, failure: Exception | None = None, **options: Any
) -> tuple[httpx.Response, str]:
    services: list[HostedProfileSessionService] = []
    transport = httpx.ASGITransport(app=full_app(root, services=services, **options))
    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        _ceremony_id, path, capability = await _begin_device(http)
        if failure is not None:

            async def fail(*args: object) -> None:
                del args
                raise failure

            services[0].accept_device_session = fail  # type: ignore[method-assign,assignment]
        response = await http.post(path, headers=_handoff_headers(capability), json=body)
    return response, capability


async def _every_handoff_path(root: Path) -> list[tuple[httpx.Response, str]]:
    answers = [
        await _handoff_once(root / "ready", sentinel_handoff()),
        await _handoff_once(root / "invalid", sentinel_handoff(extra=LOG_SENTINEL_VALUE)),
        await _handoff_once(
            root / "bad-cookie",
            sentinel_handoff(
                cookies=[
                    {
                        "name": LOG_SENTINEL_NAME,
                        "value": LOG_SENTINEL_VALUE + ";",
                        "domain": ".example.org",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ]
            ),
        ),
        await _handoff_once(
            root / "unconfirmed", sentinel_handoff(confirmed_url="https://example.org/")
        ),
        await _handoff_once(
            root / "provider",
            sentinel_handoff(),
            evidence_failure=RuntimeError(f"net::ERR at /{LOG_SENTINEL_PATH}"),
        ),
    ]
    for index, failure in enumerate(injected_failures()):
        answers.append(
            await _handoff_once(root / f"injected-{index}", sentinel_handoff(), failure=failure)
        )
    return answers


async def test_device_handoff_logs_nothing_from_the_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    answers = await _every_handoff_path(tmp_path)

    assert [response.status_code for response, _ in answers] == [
        200,
        400,
        400,
        422,
        409,
        500,
        500,
        500,
    ]
    capabilities = tuple(capability for _, capability in answers)
    assert _leaks(caplog.records, _LOG_SENTINELS + capabilities) == []
    for response, _capability in answers:
        assert not any(secret in response.text for secret in _LOG_SENTINELS)
    assert any(record.getMessage() == "device handoff failed" for record in caplog.records)


@pytest.mark.parametrize("failure", injected_failures(), ids=lambda failure: type(failure).__name__)
async def test_no_exception_leaves_the_handoff_path(tmp_path: Path, failure: Exception) -> None:
    services: list[HostedProfileSessionService] = []
    transport = httpx.ASGITransport(
        app=full_app(tmp_path / "profiles", services=services), raise_app_exceptions=True
    )

    async def fail(*args: object) -> None:
        del args
        raise failure

    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        _ceremony_id, path, capability = await _begin_device(http)
        services[0].accept_device_session = fail  # type: ignore[method-assign,assignment]
        try:
            response = await http.post(
                path, headers=_handoff_headers(capability), json=sentinel_handoff()
            )
        except Exception as escaped:
            pytest.fail(f"escaped: {type(escaped).__name__}")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "service unavailable"}
    }


async def test_the_surface_check_never_raises_into_the_application(tmp_path: Path) -> None:
    services: list[HostedProfileSessionService] = []
    transport = httpx.ASGITransport(
        app=full_app(tmp_path / "profiles", services=services), raise_app_exceptions=True
    )

    async def fail(*args: object) -> bool:
        del args
        raise injected_failures()[0]

    async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
        _ceremony_id, path, capability = await _begin_device(http)
        services[0].authenticate_surface = fail  # type: ignore[method-assign,assignment]
        response = await http.post(
            path,
            headers={**_handoff_headers(capability), "Content-Length": "999999"},
            content=b"never read",
        )

    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"


async def test_device_handoff_authenticates_before_buffering_and_logs_nothing(
    tmp_path: Path,
) -> None:
    """Gate 9 (ADR-0128): capability before body; no payload in any log record."""

    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collect(level=logging.DEBUG)
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        transport = httpx.ASGITransport(app=full_app(tmp_path / "boundary"))
        async with httpx.AsyncClient(transport=transport, base_url="https://service.test") as http:
            _ceremony_id, path, _capability = await _begin_device(http)
            unread = await http.post(
                path,
                headers={"Content-Type": "application/json", "Content-Length": "999999"},
                content=b"not parsed",
            )
        answers = await _every_handoff_path(tmp_path / "paths")
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)

    assert unread.status_code == 401
    capabilities = tuple(capability for _, capability in answers)
    assert _leaks(records, _LOG_SENTINELS + capabilities) == []
