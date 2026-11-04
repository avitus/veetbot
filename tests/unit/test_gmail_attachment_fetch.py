"""The Gmail read server fetches one named attachment within its bound (ADR-0294)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import cast

import httpx
import pytest
from mcp.types import CallToolResult

from gmail_mcp.client import GmailClient, GmailCredential
from gmail_mcp.constants import (
    ATTACHMENT_MAXIMUM_BYTES,
    GOOGLE_SCOPES,
    GOOGLE_TOKEN_ENDPOINT,
    ROSTERS,
    STABLE_FAILURE_CODES,
)
from gmail_mcp.errors import GmailError
from gmail_mcp.server import create_server

CONTENT = b"%PDF-1.7 invoice"
Handler = Callable[[httpx.Request], httpx.Response]


def _encoded(content: bytes) -> str:
    return base64.urlsafe_b64encode(content).decode().rstrip("=")


def _message() -> dict[str, object]:
    return {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _encoded(b"hello")}},
                {
                    "mimeType": "multipart/related",
                    "parts": [
                        {
                            "mimeType": "image/png",
                            "filename": "logo.png",
                            "body": {"size": 4, "data": _encoded(b"logo")},
                        }
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "body": {"size": len(CONTENT), "attachmentId": "fresh-id"},
                },
            ],
        },
    }


def _client(handler: Handler) -> GmailClient:
    credential = GmailCredential.parse(
        json.dumps(
            {
                "client_id": "client-id",
                "client_secret": "client-value",
                "refresh_token": "-".join(("fixture", "refresh", "value")),
                "scope": GOOGLE_SCOPES["read"],
            }
        ),
        expected_scope=GOOGLE_SCOPES["read"],
    )

    def wrapped(request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_TOKEN_ENDPOINT:
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return handler(request)

    return GmailClient(
        credential,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(wrapped), follow_redirects=False
        ),
    )


def _mailbox(message: dict[str, object], attachment: bytes = CONTENT) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages/m1"):
            return httpx.Response(200, json=message)
        if request.url.path.endswith("/messages/m1/attachments/fresh-id"):
            return httpx.Response(200, json={"data": _encoded(attachment)})
        return httpx.Response(404)

    return handler


async def test_an_attachment_is_fetched_by_its_listed_position_with_a_fresh_id() -> None:
    fetched = await _client(_mailbox(_message())).fetch_attachment("m1", 1, "invoice.pdf")

    assert base64.b64decode(str(fetched.pop("data_base64"))) == CONTENT
    assert fetched == {
        "thread_id": "t1",
        "message_id": "m1",
        "filename": "invoice.pdf",
        "mime_type": "application/pdf",
        "size_bytes": len(CONTENT),
    }


async def test_positions_match_the_order_a_thread_lists_attachments() -> None:
    _, listed = GmailClient._payload_content(_message()["payload"])
    assert [item["filename"] for item in listed] == ["logo.png", "invoice.pdf"]


async def test_a_small_inline_attachment_needs_no_second_request() -> None:
    fetched = await _client(_mailbox(_message())).fetch_attachment("m1", 0, "logo.png")
    assert base64.b64decode(str(fetched["data_base64"])) == b"logo"


@pytest.mark.parametrize(
    ("index", "filename"),
    [(1, "other.pdf"), (5, "invoice.pdf"), (-1, "invoice.pdf")],
)
async def test_a_position_that_does_not_name_the_file_is_refused(index: int, filename: str) -> None:
    with pytest.raises(GmailError) as refused:
        await _client(_mailbox(_message())).fetch_attachment("m1", index, filename)
    assert refused.value.code == "gmail.attachment_not_found"


async def test_a_message_gmail_no_longer_has_is_not_found() -> None:
    with pytest.raises(GmailError) as refused:
        await _client(lambda _: httpx.Response(404)).fetch_attachment("m1", 0, "logo.png")
    assert refused.value.code == "gmail.attachment_not_found"


async def test_a_declared_size_over_the_limit_never_downloads_the_file() -> None:
    message = _message()
    payload = message["payload"]
    assert isinstance(payload, dict)
    payload["parts"][2]["body"]["size"] = ATTACHMENT_MAXIMUM_BYTES + 1
    requested: list[str] = []
    mailbox = _mailbox(message)

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return mailbox(request)

    with pytest.raises(GmailError) as refused:
        await _client(handler).fetch_attachment("m1", 1, "invoice.pdf")
    assert refused.value.code == "gmail.attachment_too_large"
    assert not any("attachments" in path for path in requested)


async def test_a_decoded_file_over_the_limit_is_refused_despite_a_small_declared_size() -> None:
    oversized = b"x" * (ATTACHMENT_MAXIMUM_BYTES + 1)
    with pytest.raises(GmailError) as refused:
        await _client(_mailbox(_message(), oversized)).fetch_attachment("m1", 1, "invoice.pdf")
    assert refused.value.code == "gmail.attachment_too_large"


def test_new_refusals_are_part_of_the_closed_code_set() -> None:
    assert {"gmail.attachment_not_found", "gmail.attachment_too_large"} <= STABLE_FAILURE_CODES


async def test_only_save_attachment_is_visible_to_the_model_and_bytes_are_never_text() -> None:
    assert {"fetch_attachment", "save_attachment"} <= set(ROSTERS["read"])
    server = create_server("read", _client(_mailbox(_message())))
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert (tools["fetch_attachment"].meta or {}).get("veetbot/application-only") is True
    assert (tools["save_attachment"].meta or {}).get("veetbot/application-only") is not True

    for name in ("fetch_attachment", "save_attachment"):
        result = cast(
            CallToolResult,
            await server.call_tool(
                name, {"message_id": "m1", "attachment_index": 1, "filename": "invoice.pdf"}
            ),
        )
        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content["data_base64"]
        assert result.content == []
