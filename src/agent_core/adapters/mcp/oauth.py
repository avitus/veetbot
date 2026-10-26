"""Sign-in traffic for owner-added MCP servers through the owner MCP transport (ADR-0255).

Every request goes through the process-local ``mcp_https`` proxy, follows no
redirect, reads a bounded body, and fails with a content-free domain error:
no upstream text reaches a client, an event or a log.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from agent_core.domain.errors import (
    MCPGrantRejectedError,
    MCPServerUnavailableError,
    MCPSignInUnsupportedError,
)
from agent_core.domain.mcp import (
    MCPAuthorizationMetadata,
    MCPClientRegistration,
    MCPProbeResult,
    MCPTokenGrant,
)

CLIENT_NAME = "Veetbot"
PROTOCOL_VERSION = "2025-06-18"
_MAXIMUM_BODY_BYTES = 64 * 1024
_MAXIMUM_VALUE_LENGTH = 8192
_MAXIMUM_EXPIRY_SECONDS = 10 * 365 * 86400
_CHALLENGE_PARAMETER = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(?:"([^"]*)"|([^\s,]+))')
_AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")


def _opaque(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= _MAXIMUM_VALUE_LENGTH
        and value.isascii()
        and value.isprintable()
        and " " not in value
    )


def _https(value: object) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise MCPServerUnavailableError
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
        raise MCPServerUnavailableError
    if parsed.port not in {None, 443}:
        raise MCPServerUnavailableError
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and _opaque(item))


def bearer_challenge(header: str | None) -> dict[str, str]:
    """The parameters of a ``Bearer`` challenge in ``WWW-Authenticate``, if any."""

    if header is None:
        return {}
    scheme, _, parameters = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return {}
    return {
        name.lower(): quoted if quoted else bare
        for name, quoted, bare in _CHALLENGE_PARAMETER.findall(parameters)
    }


def _well_known(url: str, suffix: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", parsed.netloc, f"/.well-known/{suffix}{path}", "", ""))


def _authorization_server_candidates(issuer: str) -> tuple[str, ...]:
    parsed = urlsplit(issuer)
    path = parsed.path.rstrip("/")
    root = ("https", parsed.netloc)
    if not path:
        return (
            urlunsplit((*root, "/.well-known/oauth-authorization-server", "", "")),
            urlunsplit((*root, "/.well-known/openid-configuration", "", "")),
        )
    return (
        urlunsplit((*root, f"/.well-known/oauth-authorization-server{path}", "", "")),
        urlunsplit((*root, f"/.well-known/openid-configuration{path}", "", "")),
        urlunsplit((*root, f"{path}/.well-known/openid-configuration", "", "")),
    )


def _same_identifier(left: str, right: str) -> bool:
    return left.rstrip("/") == right.rstrip("/")


class HTTPMCPOAuthGateway:
    def __init__(
        self,
        *,
        proxy_url: str | None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._proxy_url = proxy_url
        self._timeout = timeout_seconds
        # Tests substitute a transport; production always dials through the proxy.
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            proxy=None if self._transport is not None else self._proxy_url,
            transport=self._transport,
            follow_redirects=False,
            timeout=self._timeout,
            headers={"User-Agent": CLIENT_NAME},
        )

    async def _send(
        self, method: str, url: str, **request: Any
    ) -> tuple[int, httpx.Headers, bytes]:
        try:
            async with self._client() as client, client.stream(method, url, **request) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAXIMUM_BODY_BYTES:
                        raise MCPServerUnavailableError
                    body.extend(chunk)
                return response.status_code, response.headers, bytes(body)
        except httpx.HTTPError:
            raise MCPServerUnavailableError from None

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        try:
            payload: object = json.loads(body)
        except (ValueError, UnicodeError):
            raise MCPServerUnavailableError from None
        if not isinstance(payload, dict):
            raise MCPServerUnavailableError
        return payload

    async def _document(self, url: str) -> dict[str, Any] | None:
        status, _headers, body = await self._send(
            "GET", url, headers={"Accept": "application/json"}
        )
        if status == 404:
            return None
        if status != 200:
            raise MCPServerUnavailableError
        return self._json(body)

    async def probe(self, url: str) -> MCPProbeResult:
        status, headers, _body = await self._send(
            "POST",
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": "1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
        if 200 <= status < 300:
            return MCPProbeResult()
        if status != 401:
            raise MCPServerUnavailableError
        challenge = bearer_challenge(headers.get("WWW-Authenticate"))
        candidates = (
            (_https(challenge["resource_metadata"]),)
            if "resource_metadata" in challenge
            else (
                _well_known(url, "oauth-protected-resource"),
                _well_known(
                    urlunsplit(("https", urlsplit(url).netloc, "", "", "")),
                    "oauth-protected-resource",
                ),
            )
        )
        resource_document: dict[str, Any] | None = None
        for candidate in candidates:
            resource_document = await self._document(candidate)
            if resource_document is not None:
                break
        if resource_document is None:
            # A 401 with no protected-resource metadata wants a credential
            # Veetbot has no way to obtain.
            raise MCPSignInUnsupportedError
        resource = resource_document.get("resource")
        if not isinstance(resource, str) or not _same_identifier(resource, url):
            raise MCPServerUnavailableError
        servers = resource_document.get("authorization_servers")
        if not isinstance(servers, list) or not servers:
            raise MCPSignInUnsupportedError
        issuer = _https(servers[0])
        server_document: dict[str, Any] | None = None
        for candidate in _authorization_server_candidates(issuer):
            server_document = await self._document(candidate)
            if server_document is not None:
                break
        if server_document is None:
            raise MCPServerUnavailableError
        declared_issuer = server_document.get("issuer")
        if not isinstance(declared_issuer, str) or not _same_identifier(declared_issuer, issuer):
            raise MCPServerUnavailableError
        response_types = server_document.get("response_types_supported")
        grant_types = server_document.get("grant_types_supported")
        if (
            (isinstance(response_types, list) and "code" not in response_types)
            or (isinstance(grant_types, list) and "authorization_code" not in grant_types)
            or "S256" not in _strings(server_document.get("code_challenge_methods_supported"))
            or "registration_endpoint" not in server_document
        ):
            raise MCPSignInUnsupportedError
        methods = _strings(server_document.get("token_endpoint_auth_methods_supported")) or (
            "client_secret_basic",
        )
        if not set(methods) & set(_AUTH_METHODS):
            raise MCPSignInUnsupportedError
        scopes = tuple(
            scope for scope in challenge.get("scope", "").split() if _opaque(scope)
        ) or _strings(resource_document.get("scopes_supported"))
        return MCPProbeResult(
            authorization=MCPAuthorizationMetadata(
                resource=resource,
                issuer=declared_issuer,
                authorization_endpoint=_https(server_document.get("authorization_endpoint")),
                token_endpoint=_https(server_document.get("token_endpoint")),
                registration_endpoint=_https(server_document.get("registration_endpoint")),
                scopes=scopes,
                token_endpoint_auth_methods=methods,
            )
        )

    async def register(
        self, metadata: MCPAuthorizationMetadata, redirect_uri: str
    ) -> MCPClientRegistration:
        method = next(
            (item for item in _AUTH_METHODS if item in metadata.token_endpoint_auth_methods),
            None,
        )
        if method is None:
            raise MCPSignInUnsupportedError
        body: dict[str, Any] = {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }
        if metadata.scopes:
            body["scope"] = " ".join(metadata.scopes)
        status, _headers, payload_body = await self._send(
            "POST",
            metadata.registration_endpoint,
            json=body,
            headers={"Accept": "application/json"},
        )
        if status in {400, 401, 403}:
            raise MCPSignInUnsupportedError
        if not 200 <= status < 300:
            raise MCPServerUnavailableError
        payload = self._json(payload_body)
        client_id = payload.get("client_id")
        client_secret = payload.get("client_secret")
        granted_method = payload.get("token_endpoint_auth_method", method)
        if (
            not _opaque(client_id)
            or granted_method not in _AUTH_METHODS
            or (client_secret is not None and not _opaque(client_secret))
            or (granted_method != "none" and client_secret is None)
        ):
            raise MCPServerUnavailableError
        return MCPClientRegistration(
            client_id=str(client_id),
            client_secret=None if granted_method == "none" else str(client_secret),
            auth_method=granted_method,
        )

    async def _grant(
        self,
        metadata: MCPAuthorizationMetadata,
        registration: MCPClientRegistration,
        form: dict[str, str],
    ) -> MCPTokenGrant:
        data = {**form, "client_id": registration.client_id, "resource": metadata.resource}
        request: dict[str, Any] = {"data": data, "headers": {"Accept": "application/json"}}
        if registration.auth_method == "client_secret_post":
            data["client_secret"] = registration.client_secret or ""
        elif registration.auth_method == "client_secret_basic":
            request["auth"] = (registration.client_id, registration.client_secret or "")
        status, _headers, body = await self._send("POST", metadata.token_endpoint, **request)
        if status in {400, 401, 403}:
            raise MCPGrantRejectedError
        if status != 200:
            raise MCPServerUnavailableError
        payload = self._json(body)
        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        refresh_token = payload.get("refresh_token")
        expires_in = payload.get("expires_in")
        if (
            not _opaque(access_token)
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or (refresh_token is not None and not _opaque(refresh_token))
            or (
                expires_in is not None
                and (
                    isinstance(expires_in, bool)
                    or not isinstance(expires_in, int | float)
                    or not 0 < expires_in <= _MAXIMUM_EXPIRY_SECONDS
                )
            )
        ):
            raise MCPServerUnavailableError
        return MCPTokenGrant(
            access_token=str(access_token),
            refresh_token=None if refresh_token is None else str(refresh_token),
            expires_in=None if expires_in is None else int(expires_in),
        )

    async def exchange(
        self,
        metadata: MCPAuthorizationMetadata,
        registration: MCPClientRegistration,
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> MCPTokenGrant:
        return await self._grant(
            metadata,
            registration,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )

    async def refresh(
        self,
        metadata: MCPAuthorizationMetadata,
        registration: MCPClientRegistration,
        *,
        refresh_token: str,
    ) -> MCPTokenGrant:
        return await self._grant(
            metadata, registration, {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
