"""Boundary contract for the isolated hosted-profile lifecycle client."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest

from agent_core.adapters.browser.hosted_profiles import HostedBrowserProfileControlPlane
from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.domain.browser import BrowserProfileControlPlaneError
from tests.contract.support import principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000e0")
BASE_URL = "https://profiles.internal.example"
OPAQUE_AUTH_VALUE = "synthetic-control-plane-auth-value"


def adapter(client: httpx.AsyncClient) -> HostedBrowserProfileControlPlane:
    return HostedBrowserProfileControlPlane(
        base_url=BASE_URL,
        credentials=MappingCredentialResolver({"browser_profile_control_plane": OPAQUE_AUTH_VALUE}),
        client=client,
    )


@pytest.mark.parametrize(
    "base_url",
    ["http://profiles.internal.example", "https://profiles.internal.example/path"],
)
async def test_hosted_control_plane_requires_one_https_service_origin(base_url: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(204))
    ) as client:
        with pytest.raises(ValueError):
            HostedBrowserProfileControlPlane(
                base_url=base_url,
                credentials=MappingCredentialResolver(
                    {"browser_profile_control_plane": OPAQUE_AUTH_VALUE}
                ),
                client=client,
            )


async def test_hosted_control_plane_sends_authenticated_scoped_idempotent_lifecycle() -> None:
    requests: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith(":provision"):
            return httpx.Response(
                201,
                json={
                    "provider_name": "hosted-isolated",
                    "provider_ref": "opaque-provider-reference",
                    "encryption_key_version": "kms-key-v3",
                },
            )
        return httpx.Response(204)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(wire), follow_redirects=False
    ) as client:
        control_plane = adapter(client)
        owner = principal()
        provisioned = await control_plane.provision(
            PROFILE_ID,
            owner,
            ("https://example.org",),
        )
        await control_plane.revoke(PROFILE_ID, owner, provisioned.provider_ref)
        await control_plane.delete(PROFILE_ID, owner, provisioned.provider_ref)

    assert provisioned.provider_name == "hosted-isolated"
    assert [request.url.path for request in requests] == [
        "/v1/browser-profiles:provision",
        f"/v1/browser-profiles/{PROFILE_ID}:revoke",
        f"/v1/browser-profiles/{PROFILE_ID}:delete",
    ]
    for request, operation in zip(
        requests,
        ("provision", "revoke", "delete"),
        strict=True,
    ):
        assert request.headers["authorization"] == f"Bearer {OPAQUE_AUTH_VALUE}"
        assert request.headers["idempotency-key"] == (f"browser-profile:{PROFILE_ID}:{operation}")
        payload = json.loads(request.content)
        assert payload["tenant_id"] == owner.tenant_id
        assert payload["principal_id"] == owner.principal_id
    assert json.loads(requests[0].content)["allowed_origins"] == ["https://example.org"]
    assert json.loads(requests[1].content)["provider_ref"] == provisioned.provider_ref


@pytest.mark.parametrize("status", [401, 403])
async def test_hosted_control_plane_maps_authentication_failure_without_body(
    status: int,
) -> None:
    diagnostic = "secret upstream diagnostic must not escape"

    async def wire(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=diagnostic)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(BrowserProfileControlPlaneError) as raised:
            await adapter(client).provision(
                PROFILE_ID,
                principal(),
                ("https://example.org",),
            )

    assert raised.value.reason == "browser_profile.control_plane_auth_failed"
    assert raised.value.retryable is False
    assert diagnostic not in str(raised.value)


@pytest.mark.parametrize("status,retryable", [(409, False), (429, True), (503, True)])
async def test_hosted_control_plane_maps_conflict_and_transient_failures(
    status: int,
    retryable: bool,
) -> None:
    async def wire(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="provider-private failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(BrowserProfileControlPlaneError) as raised:
            await adapter(client).provision(
                PROFILE_ID,
                principal(),
                ("https://example.org",),
            )

    assert raised.value.retryable is retryable
    assert "provider-private" not in str(raised.value)


async def test_hosted_control_plane_rejects_invalid_or_oversized_output() -> None:
    async def invalid(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"x" * 65_537)

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid)) as client:
        with pytest.raises(BrowserProfileControlPlaneError) as raised:
            await adapter(client).provision(
                PROFILE_ID,
                principal(),
                ("https://example.org",),
            )

    assert raised.value.reason == "browser_profile.control_plane_output_invalid"
    assert raised.value.retryable is False


async def test_hosted_control_plane_maps_transport_failure_as_retryable() -> None:
    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(BrowserProfileControlPlaneError) as raised:
            await adapter(client).provision(
                PROFILE_ID,
                principal(),
                ("https://example.org",),
            )

    assert raised.value.reason == "browser_profile.control_plane_unavailable"
    assert raised.value.retryable is True
