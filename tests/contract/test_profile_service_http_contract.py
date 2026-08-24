"""Boundary contract for the isolated profile lifecycle HTTP server."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI

from agent_core.adapters.browser.hosted_profiles import HostedBrowserProfileControlPlane
from agent_core.adapters.browser.hosted_sessions import HostedBrowserSessionControlPlane
from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.browser_control_plane.api import create_profile_service_app
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
)
from agent_core.domain.credentials import SecretValue
from tests.contract.support import NOW, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000f3")
OPAQUE_AUTH_VALUE = "synthetic-profile-service-auth-value"
PROVIDER_REF = "opaque-http-reference-000000000000000001"
RUN_ID = UUID("00000000-0000-0000-0000-0000000000f8")


class FakeRuntime:
    def __init__(self, events: list[BrowserInteractiveEvent] | None = None) -> None:
        self.origins: tuple[str, ...] = ()
        self.closed = False
        self.events = events if events is not None else []

    async def start(
        self,
        material: bytes,
        allowed_origins: tuple[str, ...],
        *,
        interactive: bool,
    ) -> None:
        del material, interactive
        self.origins = allowed_origins

    async def navigate(self, url: str) -> BrowserObservation:
        return BrowserObservation(url=url, revision="revision-1")

    async def observe(self) -> BrowserObservation:
        return BrowserObservation(url=self.origins[0], revision="revision-1")

    async def act(self, action: BrowserAction) -> BrowserObservation:
        del action
        return BrowserObservation(url=self.origins[0], revision="revision-2")

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
) -> FastAPI:
    store = FilesystemEncryptedProfileStore(
        root,
        StaticProfileKeyring(
            {"key-v1": hashlib.sha256(b"synthetic-http-key").digest()},
            current_version="key-v1",
        ),
    )
    sessions = HostedProfileSessionService(
        store,
        runtime_factory=lambda tenant_id: FakeRuntime(events),
        now=lambda: NOW,
        process_secret=b"synthetic-http-process-secret-32-bytes",
        ceremony_base_url="https://login.example.test",
    )
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
