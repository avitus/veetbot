"""Authenticated client for the isolated hosted-profile control plane."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserProfileControlPlaneError,
    BrowserProfileProvisioning,
)
from agent_core.domain.credentials import CredentialRef
from agent_core.ports.credentials import CredentialResolver

MAXIMUM_CONTROL_PLANE_RESPONSE_BYTES = 65_536
CREDENTIAL_NAME = "browser_profile_control_plane"


class HostedBrowserProfileControlPlane:
    def __init__(
        self,
        *,
        base_url: str,
        credentials: CredentialResolver,
        client: httpx.AsyncClient,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("hosted profile control plane requires one HTTPS origin")
        self._base_url = base_url.rstrip("/")
        self._credentials = credentials
        self._client = client

    @staticmethod
    def _payload(
        profile_id: UUID,
        principal: Principal,
        **values: object,
    ) -> dict[str, object]:
        return {
            "profile_id": str(profile_id),
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
            **values,
        }

    async def _post(
        self,
        path: str,
        *,
        profile_id: UUID,
        operation: str,
        payload: Mapping[str, object],
        expect_json: bool,
    ) -> dict[str, Any] | None:
        try:
            secret = await self._credentials.resolve(CredentialRef(CREDENTIAL_NAME))
        except PermissionError as exc:
            raise BrowserProfileControlPlaneError(
                "browser_profile.control_plane_auth_failed",
                retryable=False,
            ) from exc
        try:
            async with self._client.stream(
                "POST",
                self._base_url + path,
                headers={
                    "Authorization": "Bearer " + secret.reveal(),
                    "Content-Type": "application/json",
                    "Idempotency-Key": f"browser-profile:{profile_id}:{operation}",
                },
                json=dict(payload),
            ) as response:
                if response.status_code in {401, 403}:
                    raise BrowserProfileControlPlaneError(
                        "browser_profile.control_plane_auth_failed",
                        retryable=False,
                    )
                if response.status_code == 409:
                    raise BrowserProfileControlPlaneError(
                        "browser_profile.control_plane_conflict",
                        retryable=False,
                    )
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    raise BrowserProfileControlPlaneError(
                        "browser_profile.control_plane_unavailable",
                        retryable=True,
                    )
                if not 200 <= response.status_code < 300:
                    raise BrowserProfileControlPlaneError(
                        "browser_profile.control_plane_rejected",
                        retryable=False,
                    )
                if not expect_json:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAXIMUM_CONTROL_PLANE_RESPONSE_BYTES:
                        raise BrowserProfileControlPlaneError(
                            "browser_profile.control_plane_output_invalid",
                            retryable=False,
                        )
        except BrowserProfileControlPlaneError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise BrowserProfileControlPlaneError(
                "browser_profile.control_plane_unavailable",
                retryable=True,
            ) from exc
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BrowserProfileControlPlaneError(
                "browser_profile.control_plane_output_invalid",
                retryable=False,
            ) from exc
        if not isinstance(decoded, dict):
            raise BrowserProfileControlPlaneError(
                "browser_profile.control_plane_output_invalid",
                retryable=False,
            )
        return {str(key): value for key, value in decoded.items()}

    async def provision(
        self,
        profile_id: UUID,
        principal: Principal,
        allowed_origins: tuple[str, ...],
    ) -> BrowserProfileProvisioning:
        response = await self._post(
            "/v1/browser-profiles:provision",
            profile_id=profile_id,
            operation="provision",
            payload=self._payload(
                profile_id,
                principal,
                allowed_origins=list(allowed_origins),
            ),
            expect_json=True,
        )
        assert response is not None
        try:
            return BrowserProfileProvisioning.model_validate(response)
        except ValidationError as exc:
            raise BrowserProfileControlPlaneError(
                "browser_profile.control_plane_output_invalid",
                retryable=False,
            ) from exc

    async def revoke(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> None:
        await self._post(
            f"/v1/browser-profiles/{profile_id}:revoke",
            profile_id=profile_id,
            operation="revoke",
            payload=self._payload(
                profile_id,
                principal,
                provider_ref=provider_ref,
            ),
            expect_json=False,
        )

    async def delete(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
    ) -> None:
        await self._post(
            f"/v1/browser-profiles/{profile_id}:delete",
            profile_id=profile_id,
            operation="delete",
            payload=self._payload(
                profile_id,
                principal,
                provider_ref=provider_ref,
            ),
            expect_json=False,
        )
