"""The grant requests carry exactly what the authorization server needs (ADR-0152)."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest

from svp_mcp import oauth
from svp_mcp.constants import (
    AUTHORIZATION_ENDPOINT,
    LOOPBACK_REDIRECT_URI,
    REGISTRATION_ENDPOINT,
    RESOURCE,
    SCOPES,
    TOKEN_ENDPOINT,
)
from svp_mcp.errors import SvpError

SECRET = "-".join(("client", "value"))
CLIENT = oauth.Client("client-1", SECRET)
GRANT = {
    "access_token": "access-2",
    "token_type": "Bearer",
    "expires_in": 3600,
    "refresh_token": "refresh-2",
}


def _http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pkce_challenge_is_the_s256_of_its_verifier() -> None:
    verifier, challenge = oauth.pkce()

    assert 43 <= len(verifier) <= 128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert oauth.pkce()[0] != verifier


def test_authorization_url_binds_state_challenge_and_resource() -> None:
    parts = urlsplit(oauth.authorization_url("client-1", state="state-1", challenge="challenge-1"))

    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZATION_ENDPOINT
    assert {name: values[0] for name, values in parse_qs(parts.query).items()} == {
        "response_type": "code",
        "client_id": "client-1",
        "redirect_uri": LOOPBACK_REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "state": "state-1",
        "code_challenge": "challenge-1",
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }


def test_registration_asks_for_one_confidential_loopback_client() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"client_id": "client-1", "client_secret": SECRET})

    assert oauth.register(_http(handle)) == CLIENT
    assert seen[0].method == "POST"
    assert str(seen[0].url) == REGISTRATION_ENDPOINT
    assert json.loads(seen[0].content) == {
        "client_name": "Veetbot",
        "redirect_uris": [LOOPBACK_REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
        "scope": " ".join(SCOPES),
    }


@pytest.mark.parametrize(
    "payload",
    [[], {"client_id": "client-1"}, {"client_id": "", "client_secret": SECRET}],
    ids=["not-an-object", "no-secret", "empty-id"],
)
def test_a_registration_without_a_confidential_client_is_refused(payload: object) -> None:
    with pytest.raises(SvpError, match=r"^svp\.response_invalid$"):
        oauth.register(_http(lambda _request: httpx.Response(201, json=payload)))


def test_code_exchange_and_refresh_send_the_client_in_the_form() -> None:
    forms: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_ENDPOINT
        forms.append(dict(parse_qsl(request.content.decode("ascii"))))
        return httpx.Response(200, json=GRANT)

    http = _http(handle)
    grant = oauth.exchange(http, CLIENT, code="code-1", verifier="verifier-1")

    assert grant == oauth.Grant("access-2", "refresh-2", 3600)
    assert oauth.refresh(http, CLIENT, "refresh-1") == grant
    assert forms == [
        {
            "grant_type": "authorization_code",
            "code": "code-1",
            "code_verifier": "verifier-1",
            "redirect_uri": LOOPBACK_REDIRECT_URI,
            "client_id": "client-1",
            "client_secret": SECRET,
            "resource": RESOURCE,
        },
        {
            "grant_type": "refresh_token",
            "refresh_token": "refresh-1",
            "client_id": "client-1",
            "client_secret": SECRET,
            "resource": RESOURCE,
        },
    ]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "svp.credential_rejected"),
        (401, "svp.credential_rejected"),
        (403, "svp.credential_rejected"),
        (429, "svp.rate_limited"),
        (503, "svp.provider_unavailable"),
        (302, "svp.provider_rejected"),
    ],
)
def test_token_failures_are_fixed_codes_without_upstream_text(status: int, code: str) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"location": "https://elsewhere.example/token"},
            json={"error": "invalid_grant", "error_description": "private diagnostic"},
        )

    with pytest.raises(SvpError) as caught:
        oauth.refresh(_http(handle), CLIENT, "refresh-1")

    assert caught.value.code == code
    assert str(caught.value) == code


def test_an_unreachable_token_endpoint_is_reported_as_unavailable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private diagnostic", request=request)

    with pytest.raises(SvpError, match=r"^svp\.provider_unavailable$"):
        oauth.refresh(_http(handle), CLIENT, "refresh-1")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"token_type": "Bearer"},
        {"access_token": "a b", "token_type": "Bearer"},
        {"access_token": "access-2", "token_type": "mac"},
        {"access_token": "access-2", "token_type": "Bearer", "expires_in": 0},
        {"access_token": "access-2", "token_type": "Bearer", "expires_in": True},
        {"access_token": "access-2", "token_type": "Bearer", "refresh_token": 5},
    ],
    ids=[
        "array",
        "no-token",
        "spaced-token",
        "not-bearer",
        "zero-life",
        "bool-life",
        "bad-refresh",
    ],
)
def test_a_malformed_grant_is_refused(payload: object) -> None:
    with pytest.raises(SvpError, match=r"^svp\.response_invalid$"):
        oauth.refresh(
            _http(lambda _request: httpx.Response(200, json=payload)), CLIENT, "refresh-1"
        )


def test_a_grant_without_a_lifetime_or_new_refresh_token_is_short_lived() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "access-2", "token_type": "bearer"})

    assert oauth.refresh(_http(handle), CLIENT, "refresh-1") == oauth.Grant("access-2", None, 300)


def test_an_oversized_token_response_is_refused() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**GRANT, "padding": "x" * 70_000})

    with pytest.raises(SvpError, match=r"^svp\.response_invalid$"):
        oauth.refresh(_http(handle), CLIENT, "refresh-1")
