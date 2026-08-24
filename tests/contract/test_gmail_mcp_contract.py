"""Shared behavioral contract for the gmail_mcp server package.

Every mode the package serves must pass this suite against the fake Gmail
API. Milestone 18 lands the ``read`` mode first; the ``write`` and ``send``
modes join the same parametrized suite as they land, which is what makes the
contract-parity gate a single suite rather than three.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from gmail_mcp.gmail import GmailClient, StaticTokenSource
from gmail_mcp.server import MODES, build_server
from tests.contract.gmail_support import (
    ACCESS_TOKEN,
    UPSTREAM_ERROR_MARKER,
    FakeGmail,
    seeded_fake,
)

READ_ROSTER = {"search_threads", "get_thread", "list_labels"}


def read_server(fake: FakeGmail) -> Any:
    client = GmailClient(
        http=httpx.AsyncClient(transport=fake.transport, base_url="https://gmail.googleapis.com"),
        token_source=StaticTokenSource(ACCESS_TOKEN),
    )
    return build_server("read", client)


async def call(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await server.call_tool(name, arguments)
    assert not result.is_error
    payload = json.loads(result.content[0].text)
    assert isinstance(payload, dict)
    return payload


async def test_read_mode_advertises_exactly_its_roster() -> None:
    server = read_server(seeded_fake())
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == READ_ROSTER
    assert MODES["read"] == tuple(sorted(READ_ROSTER))


async def test_search_threads_returns_normalized_records() -> None:
    server = read_server(seeded_fake())
    payload = await call(server, "search_threads", {"query": "lunch"})

    assert payload["next_page_token"] is None
    (record,) = payload["threads"]
    assert record["thread_id"] == "thread-lunch"
    assert record["subject"] == "Lunch on Thursday?"
    assert "ada@example.org" in record["senders"][0]
    assert record["date"] == "Mon, 24 Aug 2026 09:00:00 +0000"
    assert "lunch" in record["snippet"].lower()
    assert "INBOX" in record["label_ids"]


async def test_search_threads_pages_disjointly() -> None:
    server = read_server(seeded_fake())
    first = await call(server, "search_threads", {"query": "", "max_results": 1})
    assert first["next_page_token"] is not None
    second = await call(
        server,
        "search_threads",
        {"query": "", "max_results": 1, "page_token": first["next_page_token"]},
    )
    assert second["next_page_token"] is None
    first_ids = {record["thread_id"] for record in first["threads"]}
    second_ids = {record["thread_id"] for record in second["threads"]}
    assert first_ids and second_ids and not (first_ids & second_ids)


async def test_get_thread_reduces_html_and_never_fetches_attachments() -> None:
    fake = seeded_fake()
    server = read_server(fake)
    payload = await call(server, "get_thread", {"thread_id": "thread-receipt"})

    (message,) = payload["messages"]
    assert "Total: $42.00" in message["body"]
    assert "<b>" not in message["body"]
    (attachment,) = message["attachments"]
    assert attachment == {
        "filename": "receipt.pdf",
        "mime_type": "application/pdf",
        "size": 52_133,
    }
    assert not any("attachment" in request.url.path for request in fake.requests)


async def test_get_thread_decodes_plain_text_bodies_and_headers() -> None:
    server = read_server(seeded_fake())
    payload = await call(server, "get_thread", {"thread_id": "thread-lunch"})

    first, second = payload["messages"]
    assert first["body"] == "Shall we get lunch at noon on Thursday?"
    assert first["sender"] == "Ada Lovelace <ada@example.org>"
    assert first["subject"] == "Lunch on Thursday?"
    assert second["label_ids"] == ["SENT"]


async def test_list_labels_returns_ids_and_names() -> None:
    server = read_server(seeded_fake())
    payload = await call(server, "list_labels", {})
    assert {"id": "Label_7", "name": "receipts", "type": "user"} in payload["labels"]


async def test_oversized_message_bodies_truncate_within_the_budget() -> None:
    from gmail_mcp.gmail import MESSAGE_BODY_CHARACTER_BUDGET

    fake = seeded_fake()
    fake.threads["thread-lunch"][0].body_text = "long " * 200_000
    server = read_server(fake)
    payload = await call(server, "get_thread", {"thread_id": "thread-lunch"})
    assert len(payload["messages"][0]["body"]) <= MESSAGE_BODY_CHARACTER_BUDGET


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "gmail.credential_rejected"),
        (403, "gmail.rejected"),
        (429, "gmail.rate_limited"),
        (500, "gmail.unavailable"),
        (503, "gmail.unavailable"),
    ],
)
async def test_upstream_failures_map_to_stable_codes(status: int, code: str) -> None:
    fake = seeded_fake()
    fake.force(
        httpx.Response(status, json={"error": {"message": f"{UPSTREAM_ERROR_MARKER} {status}"}})
    )
    server = read_server(fake)
    with pytest.raises(ToolError) as failure:
        await server.call_tool("search_threads", {"query": "lunch"})
    assert code in str(failure.value)
    assert UPSTREAM_ERROR_MARKER not in str(failure.value)


async def test_undecodable_upstream_output_is_invalid_output() -> None:
    fake = seeded_fake()
    fake.force(httpx.Response(200, content=b"<html>not json</html>"))
    server = read_server(fake)
    with pytest.raises(ToolError) as failure:
        await server.call_tool("list_labels", {})
    assert "gmail.invalid_output" in str(failure.value)


async def test_oversized_upstream_responses_are_invalid_output() -> None:
    from gmail_mcp.gmail import RESPONSE_BYTE_CEILING

    fake = seeded_fake()
    fake.force(
        httpx.Response(
            200,
            content=b'{"labels": ["' + b"x" * RESPONSE_BYTE_CEILING + b'"]}',
        )
    )
    server = read_server(fake)
    with pytest.raises(ToolError) as failure:
        await server.call_tool("list_labels", {})
    assert "gmail.invalid_output" in str(failure.value)


async def test_redirects_are_refused_not_followed() -> None:
    fake = seeded_fake()
    fake.force(httpx.Response(302, headers={"Location": "https://attacker.example/collect"}))
    server = read_server(fake)
    with pytest.raises(ToolError) as failure:
        await server.call_tool("list_labels", {})
    assert "gmail.rejected" in str(failure.value)
    assert len(fake.requests) == 1


def test_the_upstream_endpoints_are_fixed_https_constants() -> None:
    from gmail_mcp.__main__ import GMAIL_BASE_URL
    from gmail_mcp.credential import TOKEN_ENDPOINT

    assert GMAIL_BASE_URL == "https://gmail.googleapis.com"
    assert TOKEN_ENDPOINT == "https://oauth2.googleapis.com/token"


async def test_the_access_token_never_appears_in_results_or_errors() -> None:
    fake = seeded_fake()
    server = read_server(fake)
    result = await server.call_tool("search_threads", {"query": "lunch"})
    assert ACCESS_TOKEN not in "".join(block.text for block in result.content)

    fake.force(httpx.Response(500, json={"error": {"message": ACCESS_TOKEN}}))
    with pytest.raises(ToolError) as failure:
        await server.call_tool("list_labels", {})
    assert ACCESS_TOKEN not in str(failure.value)
