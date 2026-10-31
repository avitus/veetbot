"""Broker-only HTTP: check once, resolve once, dial the checked address."""

from __future__ import annotations

import asyncio
import base64
import json as json_module
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent_core.domain.credentials import CredentialRef
from agent_core.domain.jobs import JobBrokerError
from agent_core.domain.web import is_public_https_url
from agent_core.execution.egress_core import address_is_public
from agent_core.ports.credentials import CredentialResolver


async def resolve_job_host(host: str) -> tuple[str, ...]:
    """Resolve a hostname once and return distinct addresses for public-address checks."""
    rows = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(row[4][0]) for row in rows))


def _redact(value: Any, secret: str | None) -> Any:
    """Remove an echoed credential from JSON strings, keys, and nested containers."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, dict):
        return {_redact(key, secret): _redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secret) for item in value]
    return value


def _refuse_encoded_echo(value: Any, secret: str | None) -> None:
    """Refuse supported reversible encodings before any response leaves the broker."""
    if not secret:
        return
    raw = secret.encode()
    patterns = (
        base64.b64encode(raw).decode().rstrip("="),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    )
    document = json_module.dumps(value, ensure_ascii=False)
    hexadecimal = (raw.hex(), "".join(f"%{byte:02x}" for byte in raw))
    if any(pattern and pattern in document for pattern in patterns) or any(
        pattern in document.casefold() for pattern in hexadecimal
    ):
        raise JobBrokerError("job.credential_echo_refused")


class JobHTTP:
    def __init__(
        self,
        credentials: CredentialResolver,
        *,
        resolver: Callable[[str], Awaitable[tuple[str, ...]]] = resolve_job_host,
        transport: httpx.AsyncBaseTransport | None = None,
        operator_hosts: frozenset[str] | None = None,
    ) -> None:
        self._credentials = credentials
        self._resolver = resolver
        self._transport = transport
        self._operator_hosts = operator_hosts

    async def request(
        self,
        *,
        method: str,
        url: str,
        json: object,
        credential: str | None,
        maximum_response_bytes: int,
        call_id: str,
    ) -> dict[str, Any]:
        """Read bounded JSON from a checked public address with the original TLS hostname."""
        del call_id
        if not is_public_https_url(url):
            raise JobBrokerError("job.host_refused")
        parsed = urlsplit(url)
        host = parsed.hostname
        assert host is not None
        if self._operator_hosts is not None and host not in self._operator_hosts:
            raise JobBrokerError("job.host_refused")
        if method not in {"GET", "POST"}:
            raise JobBrokerError("job.invalid_arguments")
        if credential is not None:
            if credential != "github-reader" or host != "api.github.com":
                raise JobBrokerError("job.credential_refused")
            # The App token is read-only too. Refuse writes before issuing even
            # that token; GraphQL is the one read API that uses POST.
            if method == "POST" and (
                parsed.path != "/graphql"
                or not isinstance(json, dict)
                or not isinstance(json.get("query"), str)
                or re.search(r"\bmutation\b", json["query"]) is not None
            ):
                raise JobBrokerError("job.credential_refused")
        if not 1 <= maximum_response_bytes <= 48 * 1024:
            raise JobBrokerError("job.response_limit")
        encoded = None if json is None else json_module.dumps(json, allow_nan=False).encode()
        if encoded is not None and len(encoded) > 48 * 1024:
            raise JobBrokerError("job.request_limit")
        transport = self._transport or httpx.AsyncHTTPTransport(trust_env=False, retries=0)
        try:
            async with asyncio.timeout(30):
                addresses = await self._resolver(host)
                if not addresses or any(not address_is_public(address) for address in addresses):
                    raise JobBrokerError("job.address_refused")
                secret = (
                    None
                    if credential is None
                    else (await self._credentials.resolve(CredentialRef(credential))).reveal()
                )
                headers = {
                    "Host": host,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "Veetbot-Jobs/1",
                    "Content-Type": "application/json",
                }
                if secret is not None:
                    headers["Authorization"] = f"Bearer {secret}"
                # The dial uses a literal checked IP. TLS authenticates the original
                # host via httpcore's SNI override, also used for certificate checks.
                request = httpx.Request(
                    method,
                    httpx.URL(url).copy_with(host=addresses[0]),
                    headers=headers,
                    content=encoded,
                    extensions={"sni_hostname": host, "timeout": httpx.Timeout(30).as_dict()},
                )
                response = await transport.handle_async_request(request)
                try:
                    if 300 <= response.status_code < 400:
                        raise JobBrokerError("job.redirect_refused")
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise JobBrokerError("job.response_encoding_refused")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > maximum_response_bytes:
                            raise JobBrokerError("job.response_limit")
                    try:
                        document = json_module.loads(content)
                        # Reject non-finite JSON before it reaches the socket encoder.
                        json_module.dumps(document, allow_nan=False)
                    except (ValueError, UnicodeError, RecursionError):
                        raise JobBrokerError("job.response_json_invalid") from None
                    _refuse_encoded_echo(document, secret)
                    return {"status": response.status_code, "json": _redact(document, secret)}
                finally:
                    await response.aclose()
        except (httpx.HTTPError, OSError, TimeoutError):
            raise JobBrokerError("job.service_unavailable", retryable=True) from None
        finally:
            if self._transport is None:
                await transport.aclose()
