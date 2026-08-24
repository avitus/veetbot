"""The credential document parse and the refresh exchange."""

from __future__ import annotations

import json

import httpx
import pytest

from gmail_mcp.credential import TOKEN_ENDPOINT, GmailCredential, RefreshingTokenSource
from gmail_mcp.errors import GmailServerError

DOCUMENT = {
    "client_id": "client-id-1",
    "client_secret": "client-secret-1",
    "refresh_token": "refresh-token-1",
    "scope": "https://www.googleapis.com/auth/gmail.readonly",
}


def test_parse_round_trips_a_complete_document() -> None:
    credential = GmailCredential.parse(json.dumps(DOCUMENT))
    assert credential.client_id == "client-id-1"
    assert credential.scope.endswith("gmail.readonly")


@pytest.mark.parametrize("missing", sorted(DOCUMENT))
def test_parse_names_the_missing_field_without_content(missing: str) -> None:
    broken = {name: value for name, value in DOCUMENT.items() if name != missing}
    with pytest.raises(ValueError) as failure:
        GmailCredential.parse(json.dumps(broken))
    assert missing in str(failure.value)
    assert "refresh-token-1" not in str(failure.value)


def test_parse_rejects_non_json_without_echoing_it() -> None:
    with pytest.raises(ValueError) as failure:
        GmailCredential.parse("secret-looking-garbage")
    assert "secret-looking-garbage" not in str(failure.value)


def _token_source(
    responses: list[httpx.Response], clock: list[float]
) -> tuple[RefreshingTokenSource, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def wire(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    source = RefreshingTokenSource(
        GmailCredential.parse(json.dumps(DOCUMENT)),
        http=httpx.AsyncClient(transport=httpx.MockTransport(wire)),
        clock=lambda: clock[0],
    )
    return source, requests


async def test_refresh_exchanges_once_and_caches_until_near_expiry() -> None:
    clock = [0.0]
    source, requests = _token_source(
        [
            httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600}),
        ],
        clock,
    )
    assert await source.access_token() == "access-1"
    assert await source.access_token() == "access-1"
    assert len(requests) == 1
    assert str(requests[0].url) == TOKEN_ENDPOINT
    body = requests[0].read().decode()
    assert "grant_type=refresh_token" in body

    clock[0] = 3590.0  # inside the sixty-second expiry margin
    assert await source.access_token() == "access-2"
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "gmail.credential_rejected"),
        (401, "gmail.credential_rejected"),
        (429, "gmail.rate_limited"),
        (500, "gmail.unavailable"),
    ],
)
async def test_refresh_failures_are_stable_and_content_free(status: int, code: str) -> None:
    source, _ = _token_source(
        [httpx.Response(status, json={"error": "upstream-google-text refresh-token-1"})],
        [0.0],
    )
    with pytest.raises(GmailServerError) as failure:
        await source.access_token()
    assert str(failure.value) == code


async def test_a_refresh_without_an_access_token_is_credential_rejected() -> None:
    source, _ = _token_source([httpx.Response(200, json={"expires_in": 3600})], [0.0])
    with pytest.raises(GmailServerError) as failure:
        await source.access_token()
    assert str(failure.value) == "gmail.credential_rejected"
