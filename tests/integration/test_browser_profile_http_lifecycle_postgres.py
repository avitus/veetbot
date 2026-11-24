"""A website profile's whole life over the public API, on PostgreSQL.

Guards docs/plan/browser-automation.md "Profiles and authentication",
"Profile control-plane contract", "Durable profile metadata", "Device sign-in
ceremony" and "Profile API contract", and the standing-grant rule in "Policy,
approvals, and standing grants", end to end: the public routes, the
application service, the PostgreSQL metadata rows, the hosted control-plane
client and the isolated profile service itself. Only the service's browser is
replaced, by the fake runtime its own HTTP contract uses, so no Chromium runs.

- creation provisions encrypted material in the isolated service and binds
  its opaque reference in PostgreSQL; no public view carries that reference;
- a login URL outside the profile's origins is ``400 malformed_request`` and
  starts no ceremony;
- a device sign-in advances the generation when it begins, and a standing
  grant is ``409`` until its outcome is recorded;
- the verified handoff makes the profile ``ready``, which survives a restart;
- delete is allowed only after revoke, removes the provider material, and both
  are idempotent; another principal sees ``404`` throughout.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

import agent_core.bootstrap as bootstrap
from agent_core.bootstrap import Composition, build
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserProfileStatus
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import NOW
from tests.contract.test_profile_service_http_contract import (
    HANDOFF_SENTINEL,
    OPAQUE_AUTH_VALUE,
    PROVIDER_REF,
    FakeRuntime,
    full_app,
    handoff_body,
)
from tests.integration.journey_support import api_client
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration

SERVICE_URL = "https://profiles.internal.example"
ORIGIN = "https://example.org"
CREATE_KEY = "create-website-profile"


def _settings() -> Settings:
    return replace(
        database_settings(),
        browser_profile_service_url=SERVICE_URL,
        credentials=MappingProxyType(
            {"browser_profile_control_plane": SecretStr(OPAQUE_AUTH_VALUE)}
        ),
    )


class _InProcessHttpx:
    """``httpx`` as the composition imports it, with clients routed to one application."""

    def __init__(self, service: FastAPI) -> None:
        self._service = service

    def _client(self, **options: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self._service), **options)

    def __getattr__(self, name: str) -> Any:
        return self._client if name == "AsyncClient" else getattr(httpx, name)


def _reach_service_in_process(monkeypatch: pytest.MonkeyPatch, service: FastAPI) -> None:
    """Point the composition's control-plane client at the in-process service.

    The composition opens its own HTTPS client for the configured service URL;
    here that client's transport is the service application, so every request
    still crosses the real wire format and bearer check.
    """

    monkeypatch.setattr(bootstrap, "httpx", _InProcessHttpx(service))


def _someone_else() -> Principal:
    return Principal(
        tenant_id="local",
        principal_id="someone-else",
        roles={"user"},
        scopes=set(PLATFORM_SCOPES),
    )


def _profile_reader() -> Principal:
    return Principal(
        tenant_id="local",
        principal_id="local-user",
        roles={"user"},
        scopes={"browser.profile.read"},
    )


async def _stored(app: Composition, profile_id: UUID) -> Any:
    async with app.uow_factory() as uow:
        return await uow.browser_profiles.get(profile_id, app.principal)


def _grant(profile_id: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "allowed_origins": [ORIGIN],
        "action_kinds": ["click"],
        "purpose": "daily lesson",
        "starts_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
    }


async def test_a_profile_is_created_signed_in_granted_revoked_and_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create, sign in on the device, restart, grant, revoke and delete one profile."""
    material = tmp_path / "profiles"
    runtimes: list[FakeRuntime] = []
    service = full_app(material, runtimes=runtimes)
    _reach_service_in_process(monkeypatch, service)
    settings = _settings()

    async with (
        build(settings=settings, storage="postgres", fixed_clock_at=NOW) as app,
        api_client(app) as client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=service), base_url="https://login.example.test"
        ) as device,
    ):
        created = await client.post(
            "/v1/browser-profiles",
            json={"allowed_origins": [ORIGIN + "/"]},
            headers={"Idempotency-Key": CREATE_KEY},
        )
        assert created.status_code == 201, created.text
        profile = created.json()
        profile_id = profile["id"]
        assert profile["allowed_origins"] == [ORIGIN]
        assert profile["status"] == "authentication_required"
        assert profile["last_sign_in_mode"] is None
        assert "provider_ref" not in profile and PROVIDER_REF not in created.text
        replayed = await client.post(
            "/v1/browser-profiles",
            json={"allowed_origins": [ORIGIN + "/"]},
            headers={"Idempotency-Key": CREATE_KEY},
        )
        assert replayed.json()["id"] == profile_id, replayed.text
        listed = await client.get("/v1/browser-profiles")
        assert [item["id"] for item in listed.json()["items"]] == [profile_id]

        # The metadata row is bound to the service's opaque reference, and the
        # encrypted material exists only in the isolated service.
        row = await _stored(app, UUID(profile_id))
        assert row.provider_ref == PROVIDER_REF
        assert row.encryption_key_version == "key-v1"
        assert len(list(material.glob("*.profile"))) == 1

        missing_key = await client.post("/v1/browser-profiles", json={"allowed_origins": [ORIGIN]})
        assert missing_key.status_code == 400, missing_key.text
        plain_http = await client.post(
            "/v1/browser-profiles",
            json={"allowed_origins": ["http://example.org"]},
            headers={"Idempotency-Key": "plain-http-origin"},
        )
        assert plain_http.status_code == 400, plain_http.text
        assert plain_http.json()["error"]["code"] == "malformed_request"

        ceremonies = f"/v1/browser-profiles/{profile_id}/authentication-ceremonies"
        off_origin = await client.post(
            ceremonies, json={"login_url": "https://www.example.org/login", "mode": "device"}
        )
        assert off_origin.status_code == 400, off_origin.text
        assert off_origin.json()["error"]["code"] == "malformed_request"
        assert "www.example.org" not in off_origin.text
        assert (await client.get(ceremonies)).json() == []
        assert (await client.get(f"/v1/browser-profiles/{profile_id}")).json() == profile
        unknown_mode = await client.post(
            ceremonies, json={"login_url": ORIGIN + "/", "mode": "kiosk"}
        )
        assert unknown_mode.status_code == 400, unknown_mode.text

        begun = await client.post(ceremonies, json={"login_url": ORIGIN + "/", "mode": "device"})
        assert begun.status_code == 201, begun.text
        assert begun.headers["Cache-Control"] == "private, no-store"
        ceremony_id = begun.json()["id"]
        assert begun.json()["status"] == "authentication_required"
        launch = urlsplit(begun.json()["launch_url"])
        capability = parse_qs(launch.fragment)["capability"][0]
        signing_in = (await client.get(f"/v1/browser-profiles/{profile_id}")).json()
        assert signing_in["status"] == "authentication_required"
        assert signing_in["generation"] == profile["generation"] + 1
        assert signing_in["last_sign_in_mode"] == "device"
        # No ceremony view after the begin response carries the capability.
        [pending] = (await client.get(ceremonies)).json()
        assert pending["id"] == ceremony_id and pending["launch_url"] is None

        early = await client.post(
            "/v1/browser-grants", json=_grant(profile_id), headers={"Idempotency-Key": "early"}
        )
        assert early.status_code == 409, early.text

        # The owner's client hands the signed-in site session to the service.
        sealed = await device.post(
            launch.path,
            headers={
                "Content-Type": "application/json",
                "X-Browser-Ceremony-Capability": capability,
            },
            json=handoff_body(),
        )
        assert sealed.status_code == 200, sealed.text
        assert sealed.json() == {"status": "ready"}

        outcome = await client.get(f"/v1/browser-authentication-ceremonies/{ceremony_id}")
        assert outcome.status_code == 200, outcome.text
        assert outcome.json()["status"] == "ready" and outcome.json()["launch_url"] is None
        ready = (await client.get(f"/v1/browser-profiles/{profile_id}")).json()
        assert ready["status"] == "ready"
        assert ready["generation"] > signing_in["generation"]
        again = await client.get(f"/v1/browser-authentication-ceremonies/{ceremony_id}")
        assert again.json()["status"] == "ready"
        assert (await client.get(f"/v1/browser-profiles/{profile_id}")).json() == ready
        for response in (outcome, again):
            assert HANDOFF_SENTINEL not in response.text and capability not in response.text

        async with api_client(app, principal=_someone_else()) as stranger:
            for method, path in (
                ("GET", f"/v1/browser-profiles/{profile_id}"),
                ("POST", f"/v1/browser-profiles/{profile_id}/revoke"),
                ("GET", f"/v1/browser-authentication-ceremonies/{ceremony_id}"),
                ("POST", f"/v1/browser-authentication-ceremonies/{ceremony_id}/cancel"),
            ):
                hidden = await stranger.request(method, path)
                assert hidden.status_code == 404, (method, path, hidden.text)
            assert (await stranger.get("/v1/browser-profiles")).json()["items"] == []
            # Another principal's profile is indistinguishable from an unknown
            # one, and deleting an unknown profile is the idempotent no-op.
            unknown = await stranger.delete(f"/v1/browser-profiles/{profile_id}")
            assert unknown.status_code == 204, unknown.text
        assert (await client.get(f"/v1/browser-profiles/{profile_id}")).json() == ready
        assert len(list(material.glob("*.profile"))) == 1
        async with api_client(app, principal=_profile_reader()) as reader:
            assert (await reader.get(f"/v1/browser-profiles/{profile_id}")).status_code == 200
            for method, path in (
                ("POST", "/v1/browser-profiles"),
                ("POST", ceremonies),
                ("POST", f"/v1/browser-profiles/{profile_id}/revoke"),
                ("DELETE", f"/v1/browser-profiles/{profile_id}"),
            ):
                refused = await reader.request(
                    method,
                    path,
                    json={"allowed_origins": [ORIGIN], "login_url": ORIGIN + "/"},
                    headers={"Idempotency-Key": "reader"},
                )
                assert refused.status_code == 403, (method, path, refused.text)

    # A restart keeps the signed-in profile: the row is in PostgreSQL and the
    # sealed session is in the isolated service.
    async with (
        build(settings=settings, storage="postgres", fixed_clock_at=NOW) as app,
        api_client(app) as client,
    ):
        assert (await client.get(f"/v1/browser-profiles/{profile_id}")).json() == ready

        granted = await client.post(
            "/v1/browser-grants", json=_grant(profile_id), headers={"Idempotency-Key": "lesson"}
        )
        assert granted.status_code == 201, granted.text
        grant = granted.json()
        assert grant["profile_generation"] == ready["generation"]
        assert grant["approved_by"] == app.principal.principal_id
        assert grant["revoked_at"] is None
        grants = await client.get("/v1/browser-grants", params={"profile_id": profile_id})
        assert [item["id"] for item in grants.json()["items"]] == [grant["id"]]

        not_revoked = await client.delete(f"/v1/browser-profiles/{profile_id}")
        assert not_revoked.status_code == 409, not_revoked.text
        assert len(list(material.glob("*.profile"))) == 1

        revoked = await client.post(f"/v1/browser-profiles/{profile_id}/revoke")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "revoked"
        assert revoked.json()["generation"] > ready["generation"]
        row = await _stored(app, UUID(profile_id))
        assert row.status is BrowserProfileStatus.REVOKED
        repeated = await client.post(f"/v1/browser-profiles/{profile_id}/revoke")
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == revoked.json()
        # A revoked profile accepts no new standing authority.
        late = await client.post(
            "/v1/browser-grants", json=_grant(profile_id), headers={"Idempotency-Key": "late"}
        )
        assert late.status_code == 409, late.text

        deleted = await client.delete(f"/v1/browser-profiles/{profile_id}")
        assert deleted.status_code == 204, deleted.text
        assert list(material.glob("*.profile")) == []
        assert (await client.get(f"/v1/browser-profiles/{profile_id}")).status_code == 404
        assert (await client.get("/v1/browser-profiles")).json()["items"] == []
        retried = await client.delete(f"/v1/browser-profiles/{profile_id}")
        assert retried.status_code == 204, retried.text
    assert all(runtime.closed for runtime in runtimes)
