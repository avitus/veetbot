"""The Gmail server's transport keeps email-integration.md's failure and bound rules.

Token exchange failures map to the closed code set before any Gmail request; an
access token is reused until shortly before expiry; a read may re-authenticate
once, while a write that may have been dispatched reports an undetermined
outcome and is never repeated; upstream bodies and returned threads are bounded.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import httpx
import pytest

from gmail_mcp.client import GmailClient, GmailCredential
from gmail_mcp.constants import (
    GOOGLE_SCOPES,
    GOOGLE_TOKEN_ENDPOINT,
    OUTPUT_MAXIMUM_BYTES,
    UPSTREAM_MAXIMUM_BYTES,
)
from gmail_mcp.errors import GmailError

REFRESH = "-".join(("fixture", "refresh", "value"))
Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, mode: str = "read") -> GmailClient:
    credential = GmailCredential.parse(
        json.dumps(
            {
                "client_id": "client-id",
                "client_secret": "client-value",
                "refresh_token": REFRESH,
                "scope": GOOGLE_SCOPES[mode],
            }
        ),
        expected_scope=GOOGLE_SCOPES[mode],
    )
    return GmailClient(
        credential,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ),
    )


def _token(access: str = "access-1", expires_in: object = 3600) -> httpx.Response:
    return httpx.Response(200, json={"access_token": access, "expires_in": expires_in})


def _is_token(request: httpx.Request) -> bool:
    return str(request.url) == GOOGLE_TOKEN_ENDPOINT


LABELS = {"labels": [{"id": "INBOX", "name": "Inbox", "type": "system"}]}


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            httpx.Response(302, headers={"location": "https://elsewhere.example/"}),
            "provider_rejected",
        ),
        (httpx.Response(400, json={"error": "invalid_grant"}), "credential_rejected"),
        (httpx.Response(403), "credential_rejected"),
        (httpx.Response(404), "provider_rejected"),
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(503), "provider_unavailable"),
        (httpx.Response(200, content=b"not json"), "provider_output_invalid"),
        (httpx.Response(200, json={"expires_in": 3600}), "provider_output_invalid"),
        (_token(expires_in=0), "provider_output_invalid"),
        (_token(expires_in=True), "provider_output_invalid"),
        (_token(access=""), "provider_output_invalid"),
    ],
)
async def test_a_refused_token_exchange_maps_to_a_closed_code_before_any_gmail_request(
    response: httpx.Response, code: str
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response if _is_token(request) else httpx.Response(200, json=LABELS)

    client = _client(handle)
    with pytest.raises(GmailError) as refused:
        await client.list_labels()
    assert refused.value.code == f"gmail.{code}"
    assert REFRESH not in str(refused.value)
    assert [_is_token(request) for request in requests] == [True]


async def test_an_unreachable_token_endpoint_is_temporarily_unavailable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(GmailError, match=r"gmail\.provider_unavailable"):
        await _client(handle).list_labels()


@pytest.mark.parametrize(("expires_in", "exchanges"), [(3600, 1), (20, 2)])
async def test_the_access_token_is_reused_until_it_nears_expiry(
    expires_in: int, exchanges: int
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if _is_token(request):
            return _token(expires_in=expires_in)
        return httpx.Response(200, json=LABELS)

    client = _client(handle)
    await client.list_labels()
    await client.list_labels()
    assert sum(_is_token(request) for request in requests) == exchanges
    token_request = next(request for request in requests if _is_token(request))
    assert token_request.method == "POST"
    assert f"refresh_token={REFRESH}" in token_request.content.decode()


async def test_a_read_rejected_mid_session_re_authenticates_once() -> None:
    issued = iter(["stale", "fresh"])
    authorizations: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if _is_token(request):
            return _token(access=next(issued))
        authorizations.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer stale":
            return httpx.Response(401)
        return httpx.Response(200, json=LABELS)

    labels = await _client(handle).list_labels()
    assert [label["id"] for label in labels["labels"]] == ["INBOX"]
    assert authorizations == ["Bearer stale", "Bearer fresh"]


async def test_a_read_rejected_twice_is_a_credential_rejection() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _token() if _is_token(request) else httpx.Response(401)

    with pytest.raises(GmailError, match=r"gmail\.credential_rejected"):
        await _client(handle).list_labels()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401),
        httpx.Response(429),
        httpx.Response(502),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
    ids=["unauthorized", "rate-limited", "unavailable", "not-json", "not-an-object"],
)
async def test_a_dispatched_write_never_retries_and_reports_an_undetermined_outcome(
    response: httpx.Response,
) -> None:
    """A failure after a mutation left the worker cannot prove the effect did not happen."""

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _token() if _is_token(request) else response

    with pytest.raises(GmailError, match=r"gmail\.outcome_unknown"):
        await _client(handle, "write").trash_thread("thread-1")
    mutations = [request for request in requests if not _is_token(request)]
    assert [(request.method, request.url.path) for request in mutations] == [
        ("POST", "/gmail/v1/users/me/threads/thread-1/trash")
    ]
    assert sum(_is_token(request) for request in requests) == 1


@pytest.mark.parametrize(
    ("mode", "code"), [("read", "provider_output_invalid"), ("write", "outcome_unknown")]
)
async def test_an_oversized_upstream_body_is_refused(mode: str, code: str) -> None:
    oversized = b" " * (UPSTREAM_MAXIMUM_BYTES + 1)

    def handle(request: httpx.Request) -> httpx.Response:
        return _token() if _is_token(request) else httpx.Response(200, content=oversized)

    client = _client(handle, mode)
    with pytest.raises(GmailError) as refused:
        if mode == "read":
            await client.list_labels()
        else:
            await client.trash_thread("thread-1")
    assert refused.value.code == f"gmail.{code}"


def _encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _thread_handler(thread: dict[str, object]) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        return _token() if _is_token(request) else httpx.Response(200, json=thread)

    return handle


async def test_html_bodies_are_reduced_to_text_and_plain_text_wins() -> None:
    html_only = {
        "id": "html",
        "threadId": "thread-1",
        "payload": {
            "mimeType": "text/html",
            "body": {"data": _encoded("<p>Board &amp;</p><p><b>budget</b>   review</p>")},
        },
    }
    both = {
        "id": "both",
        "threadId": "thread-1",
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _encoded("Plain words.")}},
                {"mimeType": "text/html", "body": {"data": _encoded("<p>Rich words.</p>")}},
            ],
        },
    }
    thread = await _client(
        _thread_handler({"id": "thread-1", "messages": [html_only, both]})
    ).get_thread("thread-1")
    assert [message["body"] for message in thread["messages"]] == [
        "Board & budget review",
        "Plain words.",
    ]


async def test_a_thread_beyond_the_output_budget_returns_a_truncated_prefix() -> None:
    header = "x" * 8192
    messages = [
        {
            "id": f"message-{index}",
            "threadId": "thread-1",
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": name, "value": header} for name in ("Subject", "From", "To")],
                "body": {"data": _encoded("body")},
            },
        }
        for index in range(100)
    ]
    thread = await _client(_thread_handler({"id": "thread-1", "messages": messages})).get_thread(
        "thread-1"
    )
    assert thread["truncated"] is True
    returned = [message["id"] for message in thread["messages"]]
    assert 0 < len(returned) < 100
    assert returned == [f"message-{index}" for index in range(len(returned))]
    assert len(json.dumps(thread, ensure_ascii=False).encode()) <= OUTPUT_MAXIMUM_BYTES
