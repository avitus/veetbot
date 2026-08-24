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
WRITE_ROSTER = {"create_draft", "modify_labels", "trash_thread", "untrash_thread"}
SEND_ROSTER = {"send_message"}
ROSTERS = {"read": READ_ROSTER, "write": WRITE_ROSTER, "send": SEND_ROSTER}


def server_for(fake: FakeGmail, mode: str) -> Any:
    client = GmailClient(
        http=httpx.AsyncClient(transport=fake.transport, base_url="https://gmail.googleapis.com"),
        token_source=StaticTokenSource(ACCESS_TOKEN),
    )
    return build_server(mode, client)


def read_server(fake: FakeGmail) -> Any:
    return server_for(fake, "read")


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


def test_nothing_in_the_package_weakens_tls_verification() -> None:
    """httpx validates the CA chain and hostname by default; the package must
    never pass a `verify` override, so the authenticated-TLS default stands
    for both credentialed transports."""

    import pathlib

    package = pathlib.Path(__file__).resolve().parents[2] / "src" / "gmail_mcp"
    for path in sorted(package.rglob("*.py")):
        assert "verify" not in path.read_text(encoding="utf-8"), path


async def test_the_access_token_never_appears_in_results_or_errors() -> None:
    fake = seeded_fake()
    server = read_server(fake)
    result = await server.call_tool("search_threads", {"query": "lunch"})
    assert ACCESS_TOKEN not in "".join(block.text for block in result.content)

    fake.force(httpx.Response(500, json={"error": {"message": ACCESS_TOKEN}}))
    with pytest.raises(ToolError) as failure:
        await server.call_tool("list_labels", {})
    assert ACCESS_TOKEN not in str(failure.value)


@pytest.mark.parametrize("mode", sorted(ROSTERS))
async def test_every_mode_advertises_exactly_its_roster(mode: str) -> None:
    server = server_for(seeded_fake(), mode)
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == ROSTERS[mode]
    assert MODES[mode] == tuple(sorted(ROSTERS[mode]))


async def test_no_mode_exposes_permanent_deletion() -> None:
    for mode in sorted(ROSTERS):
        server = server_for(seeded_fake(), mode)
        for tool in await server.list_tools():
            assert "delete" not in tool.name
    assert not [name for name in dir(GmailClient) if "delete" in name]


async def test_create_draft_stores_a_draft_by_value() -> None:
    fake = seeded_fake()
    server = server_for(fake, "write")
    payload = await call(
        server,
        "create_draft",
        {"to": "ada@example.org", "subject": "Re: Lunch", "body": "Sounds good."},
    )
    assert payload["draft_id"]
    (draft,) = fake.drafts.values()
    assert "To: ada@example.org" in str(draft["mime"])
    assert "Sounds good." in str(draft["mime"])


async def test_create_draft_threads_as_a_reply() -> None:
    fake = seeded_fake()
    server = server_for(fake, "write")
    await call(
        server,
        "create_draft",
        {
            "to": "ada@example.org",
            "subject": "Re: Lunch",
            "body": "Sounds good.",
            "thread_id": "thread-lunch",
        },
    )
    (draft,) = fake.drafts.values()
    assert draft["thread_id"] == "thread-lunch"


async def test_modify_labels_batches_over_threads() -> None:
    fake = seeded_fake()
    server = server_for(fake, "write")
    payload = await call(
        server,
        "modify_labels",
        {
            "thread_ids": ["thread-lunch", "thread-receipt"],
            "add_label_ids": ["Label_7"],
            "remove_label_ids": ["INBOX"],
        },
    )
    assert payload == {"modified_thread_ids": ["thread-lunch", "thread-receipt"]}
    for messages in (fake.threads["thread-lunch"], fake.threads["thread-receipt"]):
        for message in messages:
            assert "INBOX" not in message.label_ids
    assert "Label_7" in fake.threads["thread-receipt"][0].label_ids


async def test_modify_labels_requires_a_change_and_caps_the_batch() -> None:
    server = server_for(seeded_fake(), "write")
    with pytest.raises(ToolError) as no_change:
        await server.call_tool("modify_labels", {"thread_ids": ["thread-lunch"]})
    assert "gmail.rejected" in str(no_change.value)

    with pytest.raises(ToolError) as oversized:
        await server.call_tool(
            "modify_labels",
            {"thread_ids": [f"thread-{n}" for n in range(26)], "add_label_ids": ["Label_7"]},
        )
    assert "gmail.rejected" in str(oversized.value)


async def test_trash_and_untrash_round_trip() -> None:
    fake = seeded_fake()
    server = server_for(fake, "write")
    await call(server, "trash_thread", {"thread_id": "thread-receipt"})
    assert "TRASH" in fake.threads["thread-receipt"][0].label_ids
    await call(server, "untrash_thread", {"thread_id": "thread-receipt"})
    assert "TRASH" not in fake.threads["thread-receipt"][0].label_ids


async def test_send_message_sends_by_value() -> None:
    fake = seeded_fake()
    server = server_for(fake, "send")
    payload = await call(
        server,
        "send_message",
        {"to": "ada@example.org", "subject": "Confirmed", "body": "See you at noon."},
    )
    assert payload["message_id"]
    (sent,) = fake.sent
    assert "To: ada@example.org" in str(sent["mime"])
    assert "Subject: Confirmed" in str(sent["mime"])
    assert "See you at noon." in str(sent["mime"])
    assert sent["thread_id"] is None


async def test_send_message_threads_as_a_reply() -> None:
    fake = seeded_fake()
    server = server_for(fake, "send")
    payload = await call(
        server,
        "send_message",
        {
            "to": "ada@example.org",
            "subject": "Re: Lunch on Thursday?",
            "body": "Running five minutes late.",
            "thread_id": "thread-lunch",
        },
    )
    assert payload["thread_id"] == "thread-lunch"
    (sent,) = fake.sent
    assert sent["thread_id"] == "thread-lunch"


@pytest.mark.parametrize(
    ("mode", "tool", "arguments"),
    [
        ("write", "trash_thread", {"thread_id": "thread-lunch"}),
        ("send", "send_message", {"to": "a@example.org", "subject": "s", "body": "b"}),
    ],
)
async def test_write_and_send_failures_are_stable_too(
    mode: str, tool: str, arguments: dict[str, Any]
) -> None:
    fake = seeded_fake()
    fake.force(httpx.Response(401, json={"error": {"message": f"{UPSTREAM_ERROR_MARKER} 401"}}))
    server = server_for(fake, mode)
    with pytest.raises(ToolError) as failure:
        await server.call_tool(tool, arguments)
    assert "gmail.credential_rejected" in str(failure.value)
    assert UPSTREAM_ERROR_MARKER not in str(failure.value)
