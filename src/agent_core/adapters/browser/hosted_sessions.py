"""Authenticated client for hosted browser sessions and login ceremonies."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserAuthenticationView,
    BrowserLease,
    BrowserObservation,
    BrowserProviderError,
    require_service_origin,
)
from agent_core.domain.credentials import CredentialRef
from agent_core.ports.credentials import CredentialResolver

MAXIMUM_SESSION_RESPONSE_BYTES = 640 * 1024
CREDENTIAL_NAME = "browser_profile_control_plane"


class HostedBrowserSessionControlPlane:
    def __init__(
        self,
        *,
        base_url: str,
        credentials: CredentialResolver,
        client: httpx.AsyncClient,
    ) -> None:
        self._base_url = require_service_origin(
            base_url,
            message="hosted browser sessions require one HTTPS service origin",
        )
        self._credentials = credentials
        self._client = client

    @staticmethod
    def _principal_payload(principal: Principal) -> dict[str, str]:
        return {
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
        }

    async def _post(
        self,
        path: str,
        *,
        payload: Mapping[str, object],
        response_model: type[BaseModel] | None,
        idempotency_key: str | None = None,
    ) -> BaseModel | None:
        try:
            secret = await self._credentials.resolve(CredentialRef(CREDENTIAL_NAME))
        except PermissionError as exc:
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=False,
            ) from exc
        headers = {
            "Authorization": "Bearer " + secret.reveal(),
            "Content-Type": "application/json",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with self._client.stream(
                "POST",
                self._base_url + path,
                headers=headers,
                json=dict(payload),
            ) as response:
                if response.status_code in {401, 403}:
                    raise BrowserProviderError(
                        "tool.browser.provider_unavailable",
                        retryable=False,
                    )
                if response.status_code == 409:
                    body = await _bounded_body(response)
                    reason = _safe_reason_code(body) or "tool.browser.profile_unavailable"
                    raise BrowserProviderError(reason, retryable=False)
                if response.status_code in {408, 425, 429} or response.status_code >= 500:
                    raise BrowserProviderError(
                        "tool.browser.provider_unavailable",
                        retryable=True,
                    )
                if not 200 <= response.status_code < 300:
                    raise BrowserProviderError(
                        "tool.browser.provider_unavailable",
                        retryable=False,
                    )
                if response_model is None:
                    return None
                body = await _bounded_body(response)
        except BrowserProviderError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise BrowserProviderError(
                "tool.browser.provider_unavailable",
                retryable=True,
            ) from exc
        try:
            return response_model.model_validate_json(body)
        except ValidationError as exc:
            raise BrowserProviderError(
                "tool.browser.output_invalid",
                retryable=False,
            ) from exc

    async def acquire(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        run_id: UUID,
        attempt_number: int,
        deadline_at: datetime,
    ) -> BrowserLease:
        result = await self._post(
            "/v1/browser-sessions:acquire",
            payload={
                "profile_id": str(profile_id),
                "provider_ref": provider_ref,
                "run_id": str(run_id),
                "attempt_number": attempt_number,
                "deadline_at": deadline_at.isoformat(),
                **self._principal_payload(principal),
            },
            response_model=BrowserLease,
            idempotency_key=(f"browser-session:{profile_id}:{run_id}:{attempt_number}:acquire"),
        )
        assert isinstance(result, BrowserLease)
        return result

    async def navigate(self, lease_ref: str, url: str) -> BrowserObservation:
        result = await self._post(
            "/v1/browser-sessions:navigate",
            payload={"lease_ref": lease_ref, "url": url},
            response_model=BrowserObservation,
        )
        assert isinstance(result, BrowserObservation)
        return result

    async def observe(self, lease_ref: str) -> BrowserObservation:
        result = await self._post(
            "/v1/browser-sessions:observe",
            payload={"lease_ref": lease_ref},
            response_model=BrowserObservation,
        )
        assert isinstance(result, BrowserObservation)
        return result

    async def act(
        self,
        lease_ref: str,
        action: BrowserAction,
        *,
        sequence: int,
    ) -> BrowserObservation:
        digest = _private_ref_digest(lease_ref)
        result = await self._post(
            "/v1/browser-sessions:act",
            payload={
                "lease_ref": lease_ref,
                "action": action.model_dump(mode="json"),
                "sequence": sequence,
            },
            response_model=BrowserObservation,
            idempotency_key=f"browser-session:{digest}:act:{sequence}",
        )
        assert isinstance(result, BrowserObservation)
        return result

    async def close(self, lease_ref: str) -> None:
        digest = _private_ref_digest(lease_ref)
        await self._post(
            "/v1/browser-sessions:close",
            payload={"lease_ref": lease_ref},
            response_model=None,
            idempotency_key=f"browser-session:{digest}:close",
        )

    async def begin_authentication(
        self,
        profile_id: UUID,
        principal: Principal,
        provider_ref: str,
        *,
        login_url: str,
    ) -> BrowserAuthenticationView:
        result = await self._post(
            "/v1/browser-authentications:begin",
            payload={
                "profile_id": str(profile_id),
                "provider_ref": provider_ref,
                "login_url": login_url,
                **self._principal_payload(principal),
            },
            response_model=BrowserAuthenticationView,
            idempotency_key=f"browser-authentication:{profile_id}:begin",
        )
        assert isinstance(result, BrowserAuthenticationView)
        return result

    async def authentication_status(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView:
        result = await self._post(
            "/v1/browser-authentications:status",
            payload={
                "ceremony_id": str(ceremony_id),
                **self._principal_payload(principal),
            },
            response_model=BrowserAuthenticationView,
        )
        assert isinstance(result, BrowserAuthenticationView)
        return result

    async def refresh_authentication(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView:
        return await self.authentication_status(ceremony_id, principal)

    async def cancel_authentication(
        self,
        ceremony_id: UUID,
        principal: Principal,
    ) -> BrowserAuthenticationView:
        result = await self._post(
            "/v1/browser-authentications:cancel",
            payload={
                "ceremony_id": str(ceremony_id),
                **self._principal_payload(principal),
            },
            response_model=BrowserAuthenticationView,
            idempotency_key=f"browser-authentication:{ceremony_id}:cancel",
        )
        assert isinstance(result, BrowserAuthenticationView)
        return result


async def _bounded_body(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAXIMUM_SESSION_RESPONSE_BYTES:
            raise BrowserProviderError("tool.browser.output_invalid", retryable=False)
    return bytes(body)


def _safe_reason_code(body: bytes) -> str | None:
    try:
        decoded: Any = json.loads(body)
        value = decoded["error"]["code"]
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(value, str) and value.startswith("tool.browser.") and len(value) <= 128:
        return value
    return None


def _private_ref_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]
