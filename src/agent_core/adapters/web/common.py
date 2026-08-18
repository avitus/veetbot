"""Shared bounded HTTP behavior for web-provider adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from agent_core.domain.credentials import CredentialRef
from agent_core.domain.web import WebProviderError
from agent_core.ports.credentials import CredentialResolver

MAXIMUM_RESPONSE_BYTES = 2 * 1024 * 1024


async def post_json(
    client: httpx.AsyncClient,
    credentials: CredentialResolver,
    *,
    credential_name: str,
    url: str,
    payload: Mapping[str, object],
) -> dict[str, Any]:
    """POST one bounded JSON request without exposing upstream response text."""

    try:
        secret = await credentials.resolve(CredentialRef(credential_name))
    except PermissionError as exc:
        raise WebProviderError("tool.web.auth_failed", retryable=False) from exc
    try:
        async with client.stream(
            "POST",
            url,
            headers={
                "Authorization": "Bearer " + secret.reveal(),
                "Content-Type": "application/json",
            },
            json=dict(payload),
        ) as response:
            if response.status_code in {401, 403}:
                raise WebProviderError("tool.web.auth_failed", retryable=False)
            if response.status_code in {408, 425, 429} or response.status_code >= 500:
                raise WebProviderError("tool.web.provider_unavailable", retryable=True)
            if response.status_code < 200 or response.status_code >= 300:
                raise WebProviderError("tool.web.provider_rejected", retryable=False)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAXIMUM_RESPONSE_BYTES:
                    raise WebProviderError("tool.web.output_invalid", retryable=False)
    except WebProviderError:
        raise
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise WebProviderError("tool.web.provider_unavailable", retryable=True) from exc

    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WebProviderError("tool.web.output_invalid", retryable=False) from exc
    if not isinstance(decoded, dict):
        raise WebProviderError("tool.web.output_invalid", retryable=False)
    return {str(key): value for key, value in decoded.items()}


def required_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WebProviderError("tool.web.output_invalid", retryable=False)
    return value


def required_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise WebProviderError("tool.web.output_invalid", retryable=False)
    return value


def required_string(value: object) -> str:
    if not isinstance(value, str):
        raise WebProviderError("tool.web.output_invalid", retryable=False)
    return value


def optional_string(value: object, *, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback
