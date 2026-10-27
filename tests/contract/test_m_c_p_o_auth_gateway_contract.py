"""MCPOAuthGateway contract (ADR-0255): discovery, registration and grants.

The HTTP adapter is checked against authored documents shaped like the ones
MagicPath published; every failure is a content-free domain error.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from agent_core.adapters.mcp.oauth import HTTPMCPOAuthGateway, bearer_challenge
from agent_core.domain.errors import (
    MCPGrantRejectedError,
    MCPServerUnavailableError,
    MCPSignInUnsupportedError,
)
from agent_core.domain.mcp import MCPAuthorizationMetadata, MCPClientRegistration

CALLBACK = "https://api.veetbot.example/v1/mcp-oauth/callback"
MCP_URL = "https://api.magicpath.ai/mcp"
SCHEME = "Bearer"
# The documents MagicPath published on 2026-10-26, as the probe found them.
RESOURCE_METADATA = "https://api.magicpath.ai/.well-known/oauth-protected-resource/mcp"
CHALLENGE = (
    f'{SCHEME} resource_metadata="{RESOURCE_METADATA}", '
    'error="invalid_token", error_description="A valid MagicPath MCP access token is required."'
)
PROTECTED_RESOURCE = {
    "resource": MCP_URL,
    "authorization_servers": ["https://api.magicpath.ai/"],
    "scopes_supported": ["magicpath:read", "magicpath:write"],
    "resource_name": "MagicPath MCP",
}
AUTHORIZATION_SERVER = {
    "issuer": "https://api.magicpath.ai/",
    "authorization_endpoint": "https://api.magicpath.ai/authorize",
    "response_types_supported": ["code"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint": "https://api.magicpath.ai/token",
    "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "scopes_supported": ["magicpath:read", "magicpath:write"],
    "revocation_endpoint": "https://api.magicpath.ai/revoke",
    "registration_endpoint": "https://api.magicpath.ai/register",
}
METADATA = MCPAuthorizationMetadata(
    resource=MCP_URL,
    issuer="https://api.magicpath.ai/",
    authorization_endpoint="https://api.magicpath.ai/authorize",
    token_endpoint=str(AUTHORIZATION_SERVER["token_endpoint"]),
    registration_endpoint="https://api.magicpath.ai/register",
    scopes=("magicpath:read", "magicpath:write"),
    token_endpoint_auth_methods=("client_secret_post", "none"),
)


class _Server:
    """An authored authorization server; records every request it receives."""

    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get((request.method, str(request.url)))
        return response if response is not None else httpx.Response(404)


def _gateway(server: _Server) -> HTTPMCPOAuthGateway:
    return HTTPMCPOAuthGateway(proxy_url=None, transport=httpx.MockTransport(server))


def _magicpath(**replacements: httpx.Response) -> _Server:
    routes = {
        ("POST", MCP_URL): httpx.Response(
            401, headers={"WWW-Authenticate": CHALLENGE}, json={"error": "token required"}
        ),
        (
            "GET",
            "https://api.magicpath.ai/.well-known/oauth-protected-resource/mcp",
        ): httpx.Response(200, json=PROTECTED_RESOURCE),
        ("GET", "https://api.magicpath.ai/.well-known/oauth-authorization-server"): httpx.Response(
            200, json=AUTHORIZATION_SERVER
        ),
    }
    for key, response in replacements.items():
        routes[
            {
                "resource": (
                    "GET",
                    "https://api.magicpath.ai/.well-known/oauth-protected-resource/mcp",
                ),
                "server": (
                    "GET",
                    "https://api.magicpath.ai/.well-known/oauth-authorization-server",
                ),
                "probe": ("POST", MCP_URL),
            }[key]
        ] = response
    return _Server(routes)


def test_a_bearer_challenge_is_parsed_without_its_description() -> None:
    assert bearer_challenge(CHALLENGE)["resource_metadata"].endswith("/mcp")
    assert bearer_challenge('Bearer scope="a b", realm=x') == {"scope": "a b", "realm": "x"}
    assert bearer_challenge("Basic realm=x") == {}
    assert bearer_challenge(None) == {}


async def test_the_magicpath_probe_finds_its_sign_in() -> None:
    server = _magicpath()
    result = await _gateway(server).probe(MCP_URL)
    assert result.authorization == METADATA
    initialize = json.loads(server.requests[0].content)
    assert initialize["method"] == "initialize"
    assert "Authorization" not in server.requests[0].headers


async def test_an_open_server_probes_open_and_an_unreachable_one_is_unavailable() -> None:
    open_server = _Server({("POST", MCP_URL): httpx.Response(200, json={"result": {}})})
    assert (await _gateway(open_server).probe(MCP_URL)).authorization is None
    for status in (403, 404, 500, 302):
        server = _magicpath(probe=httpx.Response(status, headers={"Location": "https://x.test"}))
        with pytest.raises(MCPServerUnavailableError):
            await _gateway(server).probe(MCP_URL)
    # A redirect is never followed.
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("replacement", "error"),
    [
        ({"resource": httpx.Response(404)}, MCPSignInUnsupportedError),
        (
            {
                "resource": httpx.Response(
                    200, json={**PROTECTED_RESOURCE, "resource": "https://x.test/"}
                )
            },
            MCPServerUnavailableError,
        ),
        (
            {
                "resource": httpx.Response(
                    200, json={**PROTECTED_RESOURCE, "authorization_servers": []}
                )
            },
            MCPSignInUnsupportedError,
        ),
        (
            {
                "server": httpx.Response(
                    200,
                    json={**AUTHORIZATION_SERVER, "code_challenge_methods_supported": ["plain"]},
                )
            },
            MCPSignInUnsupportedError,
        ),
        (
            {
                "server": httpx.Response(
                    200,
                    json={
                        key: value
                        for key, value in AUTHORIZATION_SERVER.items()
                        if key != "registration_endpoint"
                    },
                )
            },
            MCPSignInUnsupportedError,
        ),
        (
            {
                "server": httpx.Response(
                    200, json={**AUTHORIZATION_SERVER, "issuer": "https://evil.test/"}
                )
            },
            MCPServerUnavailableError,
        ),
        (
            {
                "server": httpx.Response(
                    200,
                    json={
                        **AUTHORIZATION_SERVER,
                        "token_endpoint": "http://api.magicpath.ai/token",
                    },
                )
            },
            MCPServerUnavailableError,
        ),
        ({"server": httpx.Response(200, content=b"[]")}, MCPServerUnavailableError),
        ({"server": httpx.Response(200, content=b"x" * (65 * 1024))}, MCPServerUnavailableError),
    ],
)
async def test_a_sign_in_veetbot_cannot_trust_or_perform_is_refused(
    replacement: dict[str, httpx.Response], error: type[Exception]
) -> None:
    with pytest.raises(error):
        await _gateway(_magicpath(**replacement)).probe(MCP_URL)


async def test_registration_exchange_and_refresh_send_what_the_server_offers() -> None:
    server = _Server(
        {
            ("POST", "https://api.magicpath.ai/register"): httpx.Response(
                201,
                json={
                    "client_id": "client-1",
                    "client_secret": "secret-1",
                    "token_endpoint_auth_method": "client_secret_post",
                },
            ),
            ("POST", "https://api.magicpath.ai/token"): httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "refresh-1",
                },
            ),
        }
    )
    gateway = _gateway(server)
    registration = await gateway.register(METADATA, CALLBACK)
    assert registration == MCPClientRegistration(
        client_id="client-1", client_secret="secret-1", auth_method="client_secret_post"
    )
    registered = json.loads(server.requests[0].content)
    assert registered["redirect_uris"] == [CALLBACK]
    assert registered["grant_types"] == ["authorization_code", "refresh_token"]
    # "none" is not first in the server's list, but Veetbot prefers no secret.
    assert registered["token_endpoint_auth_method"] == "none"
    assert registered["scope"] == "magicpath:read magicpath:write"
    grant = await gateway.exchange(
        METADATA, registration, code="code-1", verifier="verifier-1", redirect_uri=CALLBACK
    )
    assert (grant.access_token, grant.refresh_token, grant.expires_in) == (
        "access-1",
        "refresh-1",
        3600,
    )
    form = parse_qs(server.requests[1].content.decode())
    assert {name: values[0] for name, values in form.items()} == {
        "grant_type": "authorization_code",
        "code": "code-1",
        "code_verifier": "verifier-1",
        "redirect_uri": CALLBACK,
        "client_id": "client-1",
        "client_secret": "secret-1",
        "resource": MCP_URL,
    }
    await gateway.refresh(METADATA, registration, refresh_token="refresh-1")
    refreshed = parse_qs(server.requests[2].content.decode())
    assert refreshed["grant_type"] == ["refresh_token"]
    assert refreshed["refresh_token"] == ["refresh-1"]
    basic = registration.model_copy(update={"auth_method": "client_secret_basic"})
    await gateway.refresh(METADATA, basic, refresh_token="refresh-1")
    assert server.requests[3].headers["Authorization"].startswith("Basic ")
    assert "client_secret" not in parse_qs(server.requests[3].content.decode())


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(400, json={"error": "invalid_grant"}), MCPGrantRejectedError),
        (httpx.Response(401), MCPGrantRejectedError),
        (httpx.Response(503), MCPServerUnavailableError),
        (
            httpx.Response(200, json={"access_token": "a", "token_type": "mac"}),
            MCPServerUnavailableError,
        ),
        (httpx.Response(200, json={"token_type": "Bearer"}), MCPServerUnavailableError),
        (
            httpx.Response(
                200, json={"access_token": "a", "token_type": "Bearer", "expires_in": -1}
            ),
            MCPServerUnavailableError,
        ),
    ],
)
async def test_a_grant_failure_carries_no_upstream_text(
    response: httpx.Response, error: type[Exception]
) -> None:
    server = _Server({("POST", "https://api.magicpath.ai/token"): response})
    registration = MCPClientRegistration(client_id="client-1")
    with pytest.raises(error) as raised:
        await _gateway(server).refresh(METADATA, registration, refresh_token="refresh-1")
    assert "invalid_grant" not in str(raised.value)


async def test_the_gateway_dials_only_through_its_proxy() -> None:
    gateway = HTTPMCPOAuthGateway(proxy_url="http://127.0.0.1:65000")
    async with gateway._client() as client:
        # httpx mounts the proxied transport for every URL.
        pools = [
            type(getattr(mounted, "_pool", None)).__name__ for mounted in client._mounts.values()
        ]
        assert pools == ["AsyncHTTPProxy"]
