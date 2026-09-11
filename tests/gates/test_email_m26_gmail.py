"""Milestone 26 bounded Gmail synchronization and reply contracts."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.mcp import MCPRemoteTool
from agent_core.mcp.configuration import email_server_configs
from agent_core.mcp.mapping import map_discovered_tools
from agent_core.model.tool_definitions import tool_definition
from agent_core.tools.current_time import CurrentTimeTool
from gmail_mcp.client import GmailClient
from gmail_mcp.constants import GOOGLE_TOKEN_ENDPOINT, OUTPUT_MAXIMUM_BYTES, ROSTERS
from gmail_mcp.errors import GmailError
from gmail_mcp.server import create_server
from tests.contract.support import NOW
from tests.gates.test_email_m18 import _credential, _thread

NEW_READ_TOOLS = {"get_profile", "sync_changes", "get_thread_page", "get_message_body"}


class Mailbox:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.revision = "100"
        self.history_status = 200
        self.messages: list[dict[str, Any]] = []
        for index in range(3):
            raw = _thread()["messages"]
            assert isinstance(raw, list)
            message = dict(raw[0])
            message["id"] = f"message-{index}"
            message["historyId"] = "100"
            message["labelIds"] = ["SENT"] if index == 0 else ["INBOX", "UNREAD"]
            payload = dict(message["payload"])
            payload["headers"] = [
                *payload["headers"],
                {"name": "Reply-To", "value": "Reply Person <reply@example.test>"},
                {"name": "Message-ID", "value": f"<message-{index}@example.test>"},
                {"name": "In-Reply-To", "value": "<parent@example.test>"},
                {"name": "References", "value": "<ancestor@example.test> <parent@example.test>"},
            ]
            message["payload"] = payload
            self.messages.append(message)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == GOOGLE_TOKEN_ENDPOINT:
            return httpx.Response(200, json={"access_token": "private-access", "expires_in": 3600})
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(
                200,
                json={
                    "emailAddress": "owner@example.test",
                    "historyId": self.revision,
                    "messagesTotal": 3,
                    "threadsTotal": 1,
                },
            )
        if path.endswith("/history"):
            if self.history_status != 200:
                return httpx.Response(self.history_status, json={"error": "private upstream text"})
            return httpx.Response(
                200,
                json={
                    "historyId": self.revision,
                    "nextPageToken": "provider-next",
                    "history": [
                        {
                            "id": "99",
                            "messages": [{"id": "added", "threadId": "thread-1"}],
                            "messagesAdded": [{"message": {"id": "added", "threadId": "thread-1"}}],
                            "messagesDeleted": [
                                {"message": {"id": "deleted", "threadId": "thread-2"}}
                            ],
                            "labelsAdded": [
                                {
                                    "message": {"id": "labeled", "threadId": "thread-3"},
                                    "labelIds": ["SENT"],
                                }
                            ],
                            "labelsRemoved": [
                                {
                                    "message": {"id": "unlabeled", "threadId": "thread-4"},
                                    "labelIds": ["UNREAD"],
                                }
                            ],
                        }
                    ],
                },
            )
        if path.endswith("/threads/thread-1"):
            return httpx.Response(
                200,
                json={
                    "id": "thread-1",
                    "historyId": self.revision,
                    "messages": self.messages,
                },
            )
        if "/messages/message-" in path:
            number = int(path.rsplit("-", 1)[-1])
            return httpx.Response(200, json=self.messages[number])
        if path.endswith("/messages/send"):
            return httpx.Response(200, json={"id": "sent-1", "threadId": "thread-1"})
        if path.endswith("/drafts"):
            return httpx.Response(
                200,
                json={"id": "draft-1", "message": {"id": "draft-message", "threadId": "thread-1"}},
            )
        return httpx.Response(404, json={"error": "private upstream text"})


def client(mailbox: Mailbox, mode: str = "read") -> GmailClient:
    return GmailClient(
        _credential(mode), http_client=httpx.AsyncClient(transport=httpx.MockTransport(mailbox))
    )


async def invoke(
    mailbox: Mailbox, name: str, arguments: dict[str, Any], mode: str = "read"
) -> CallToolResult:
    server = create_server(mode, client(mailbox, mode))
    assert name in {tool.name for tool in await server.list_tools()}, (
        f"missing documented Gmail tool {name}"
    )
    result = await server.call_tool(name, arguments)
    assert isinstance(result, CallToolResult)
    return result


@pytest.mark.parametrize("mode", ["read", "write", "send"])
async def test_m26_read_extensions_keep_the_three_server_boundary(mode: str) -> None:
    server = create_server(mode, client(Mailbox(), mode))
    names = {tool.name for tool in await server.list_tools()}
    assert names == set(ROSTERS[mode])
    assert names & NEW_READ_TOOLS == (NEW_READ_TOOLS if mode == "read" else set())


async def test_m26_profile_verifies_only_provider_primary_identity() -> None:
    result = await invoke(Mailbox(), "get_profile", {})
    assert result.is_error is False
    assert result.structured_content == {
        "schema_version": 1,
        "email_address": "owner@example.test",
        "verified_addresses": ["owner@example.test"],
        "history_id": "100",
        "messages_total": 3,
        "threads_total": 1,
    }


async def test_m26_history_preserves_all_change_kinds_and_provider_cursor() -> None:
    mailbox = Mailbox()
    result = await invoke(
        mailbox,
        "sync_changes",
        {"start_history_id": "90", "max_results": 10, "page_token": "prior"},
    )
    assert result.is_error is False
    output = result.structured_content
    assert output is not None
    assert output["schema_version"] == 1
    assert output["history_id"] == "100"
    assert output["next_page_token"] == "provider-next"
    assert output["resync_required"] is False
    assert [item["kind"] for item in output["changes"]] == [
        "message_added",
        "message_deleted",
        "labels_added",
        "labels_removed",
    ]
    assert output["changes"][2] == {
        "history_id": "99",
        "kind": "labels_added",
        "message_id": "labeled",
        "thread_id": "thread-3",
        "label_ids": ["SENT"],
    }
    request = mailbox.requests[-1]
    assert request.url.params["startHistoryId"] == "90"
    assert request.url.params["maxResults"] == "10"
    assert request.url.params["pageToken"] == "prior"


async def test_m26_expired_history_requires_resync_without_upstream_error_text() -> None:
    mailbox = Mailbox()
    mailbox.history_status = 404
    result = await invoke(mailbox, "sync_changes", {"start_history_id": "1"})
    assert result.is_error is False
    assert result.structured_content == {
        "schema_version": 1,
        "resync_required": True,
        "history_id": None,
        "next_page_token": None,
        "changes": [],
    }
    assert "private upstream" not in str(result)


async def test_m26_thread_pages_are_complete_and_carry_reply_provenance() -> None:
    mailbox = Mailbox()
    first = await invoke(mailbox, "get_thread_page", {"thread_id": "thread-1", "max_messages": 2})
    assert first.is_error is False
    output = first.structured_content
    assert output is not None
    assert output["total_messages"] == 3
    assert output["returned_messages"] == 2
    assert output["history_id"] == "100"
    assert output["complete"] is False
    assert output["source_changed"] is False
    message = output["messages"][0]
    assert message["reply_to"] == "Reply Person <reply@example.test>"
    assert message["message_id_header"] == "<message-0@example.test>"
    assert message["in_reply_to"] == "<parent@example.test>"
    assert message["references"] == "<ancestor@example.test> <parent@example.test>"
    assert message["internal_date"] == "1700000000000"
    assert message["direction"] == "sent"
    assert message["body_complete"] is True
    second = await invoke(
        mailbox,
        "get_thread_page",
        {"thread_id": "thread-1", "max_messages": 2, "page_token": output["next_page_token"]},
    )
    rest = second.structured_content
    assert rest is not None
    assert rest["complete"] is True
    assert rest["next_page_token"] is None
    assert [item["id"] for item in [*output["messages"], *rest["messages"]]] == [
        "message-0",
        "message-1",
        "message-2",
    ]


async def test_m26_thread_continuation_refuses_changed_source() -> None:
    mailbox = Mailbox()
    first = await invoke(mailbox, "get_thread_page", {"thread_id": "thread-1", "max_messages": 1})
    assert first.structured_content is not None
    mailbox.revision = "101"
    result = await invoke(
        mailbox,
        "get_thread_page",
        {"thread_id": "thread-1", "page_token": first.structured_content["next_page_token"]},
    )
    assert result.structured_content is not None
    assert result.structured_content["source_changed"] is True
    assert result.structured_content["messages"] == []
    assert result.structured_content["complete"] is False


async def test_m26_body_continuation_preserves_unicode_and_revision() -> None:
    mailbox = Mailbox()
    body = "😀 café 東京\n" * 500
    mailbox.messages[0]["payload"] = {
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
    }
    chunks: list[str] = []
    offset = 0
    while True:
        result = await invoke(
            mailbox,
            "get_message_body",
            {
                "message_id": "message-0",
                "offset": offset,
                "max_bytes": 1024,
                "expected_history_id": "100",
            },
        )
        assert result.is_error is False
        output = result.structured_content
        assert output is not None
        assert len(json.dumps(output).encode()) < OUTPUT_MAXIMUM_BYTES
        chunks.append(output["body"])
        if output["complete"]:
            break
        assert output["next_offset"] > offset
        offset = output["next_offset"]
    assert "".join(chunks) == body


@pytest.mark.parametrize("operation,mode", [("send_message", "send"), ("create_draft", "write")])
async def test_m26_outbound_reply_headers_are_encoded_by_value(operation: str, mode: str) -> None:
    mailbox = Mailbox()
    server = create_server(mode, client(mailbox, mode))
    schema = next(tool.input_schema for tool in await server.list_tools() if tool.name == operation)
    assert "in_reply_to" in schema["properties"], (
        "reply headers are not accepted by the documented tool"
    )
    result = await server.call_tool(
        operation,
        {
            "to": "reply@example.test",
            "subject": "Re: Contract subject",
            "body": "Agreed.",
            "thread_id": "thread-1",
            "in_reply_to": "<parent@example.test>",
            "references": "<ancestor@example.test> <parent@example.test>",
        },
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    request = json.loads(mailbox.requests[-1].content)
    envelope = request.get("message", request)
    parsed = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(envelope["raw"] + "=" * (-len(envelope["raw"]) % 4))
    )
    assert parsed["In-Reply-To"] == "<parent@example.test>"
    assert parsed["References"] == "<ancestor@example.test> <parent@example.test>"
    assert envelope["threadId"] == "thread-1"


async def test_m26_reply_header_injection_is_rejected_before_any_request() -> None:
    mailbox = Mailbox()
    result = await invoke(
        mailbox,
        "send_message",
        {
            "to": "reply@example.test",
            "subject": "Re: Contract subject",
            "body": "Agreed.",
            "in_reply_to": "<parent@example.test>\r\nBcc: intruder@example.test",
        },
        "send",
    )
    assert result.is_error is True
    assert "gmail.arguments_invalid" in str(result)
    assert mailbox.requests == []


@pytest.mark.parametrize(
    "field,value",
    [("in_reply_to", "not-a-message-id"), ("references", "<valid@example.test> invented")],
)
async def test_m26_invalid_reply_identifiers_are_rejected_before_dispatch(
    field: str, value: str
) -> None:
    mailbox = Mailbox()
    result = await invoke(
        mailbox,
        "send_message",
        {"to": "reply@example.test", "subject": "Reply", "body": "Hello", field: value},
        "send",
    )
    assert result.is_error is True
    assert mailbox.requests == []


async def test_m26_oversized_headers_are_marked_incomplete_not_safe_to_reply() -> None:
    mailbox = Mailbox()
    mailbox.messages[0]["payload"]["headers"].append(
        {"name": "References", "value": "<old@example.test> " * 1000}
    )
    result = await invoke(mailbox, "get_thread_page", {"thread_id": "thread-1", "max_messages": 1})
    assert result.structured_content is not None
    assert result.structured_content["messages"][0].get("headers_complete") is False


async def test_m26_attachment_backed_inline_body_is_explicitly_incomplete() -> None:
    mailbox = Mailbox()
    mailbox.messages[0]["payload"] = {
        "mimeType": "text/plain",
        "body": {"attachmentId": "not-downloaded", "size": 2000000},
    }
    page = await invoke(mailbox, "get_thread_page", {"thread_id": "thread-1", "max_messages": 1})
    assert page.structured_content is not None
    assert page.structured_content["messages"][0]["body_complete"] is False
    body = await invoke(mailbox, "get_message_body", {"message_id": "message-0"})
    assert body.structured_content is not None
    assert body.structured_content["body_available"] is False
    assert body.structured_content["complete"] is False
    assert not any("/attachments/" in request.url.path for request in mailbox.requests)


async def test_m26_body_continuation_does_not_mix_revisions() -> None:
    mailbox = Mailbox()
    mailbox.messages[0]["historyId"] = "101"
    result = await invoke(
        mailbox,
        "get_message_body",
        {"message_id": "message-0", "offset": 5, "expected_history_id": "100"},
    )
    assert result.structured_content is not None
    assert result.structured_content["source_changed"] is True
    assert result.structured_content["body"] == ""
    assert result.structured_content["complete"] is False


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "gmail.credential_rejected"),
        (429, "gmail.rate_limited"),
        (500, "gmail.provider_unavailable"),
    ],
)
async def test_m26_history_failures_preserve_stable_retry_taxonomy(status: int, code: str) -> None:
    mailbox = Mailbox()
    mailbox.history_status = status
    result = await invoke(mailbox, "sync_changes", {"start_history_id": "90"})
    assert result.is_error is True
    assert code in str(result)
    assert "private upstream" not in str(result)


@pytest.mark.parametrize(
    "arguments", [{"max_results": 0}, {"max_results": 101}, {"start_history_id": "invalid"}]
)
async def test_m26_history_arguments_fail_before_network(arguments: dict[str, Any]) -> None:
    mailbox = Mailbox()
    # Direct calls exercise transport-side validation independently of the SDK's schema rejection.
    with pytest.raises(GmailError, match=r"gmail\.arguments_invalid"):
        await client(mailbox).sync_changes(**{"start_history_id": "90", **arguments})
    assert mailbox.requests == []


async def test_m26_thread_token_is_bound_to_the_requested_thread() -> None:
    mailbox = Mailbox()
    first = await invoke(mailbox, "get_thread_page", {"thread_id": "thread-1", "max_messages": 1})
    assert first.structured_content is not None
    mailbox.requests.clear()
    result = await invoke(
        mailbox,
        "get_thread_page",
        {"thread_id": "thread-other", "page_token": first.structured_content["next_page_token"]},
    )
    assert result.is_error is True
    assert mailbox.requests == []


async def test_m26_malformed_message_timestamp_is_provider_error() -> None:
    mailbox = Mailbox()
    mailbox.messages[0]["internalDate"] = "not-a-timestamp"
    result = await invoke(mailbox, "get_thread_page", {"thread_id": "thread-1"})
    assert result.is_error is True
    assert "gmail.provider_output_invalid" in str(result)


async def test_m26_sync_tools_are_application_only_without_changing_policy() -> None:
    server = create_server("read", client(Mailbox()))
    tools = await server.list_tools()
    for tool in tools:
        assert ((tool.meta or {}).get("veetbot/application-only") is True) == (
            tool.name in NEW_READ_TOOLS
        )
    declared = tuple(
        MCPRemoteTool.model_validate(
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.input_schema,
                "application_only": (tool.meta or {}).get("veetbot/application-only") is True,
            }
        )
        for tool in tools
    )
    config = next(row for row in email_server_configs("local") if row.server_id == "gmail_read")
    report = map_discovered_tools(config, declared)
    assert len(report.accepted) == len(tools)
    for mapped in report.accepted:
        assert mapped.spec.model_dump().get("model_visible") == (
            mapped.remote_name not in NEW_READ_TOOLS
        )
        assert mapped.spec.side_effect == config.side_effect
        assert mapped.spec.required_scopes == set(config.required_scopes)


def test_m26_wire_schema_omits_only_redundant_generated_titles() -> None:
    schema = {
        "type": "object",
        "title": "current_timeArguments",
        "properties": {
            "reply_to": {"type": "string", "title": "Reply To"},
            "origin": {"type": "string", "title": "Departure airport"},
            "nested": {
                "type": "object",
                "properties": {"title": {"type": "string", "title": "Title"}},
            },
        },
    }
    spec = CurrentTimeTool(FixedClock(NOW)).spec.model_copy(update={"input_schema": schema})
    result = tool_definition(spec)["parameters"]
    assert "title" not in result
    assert "title" not in result["properties"]["reply_to"]
    assert result["properties"]["origin"]["title"] == "Departure airport"
    assert result["properties"]["nested"]["properties"]["title"] == {"type": "string"}
    assert spec.input_schema == schema


@pytest.mark.parametrize("provider_token", ["p" * 4096, "\x01" * 4096], ids=["opaque", "escaped"])
async def test_m26_history_normalized_changes_are_bounded_and_overflow_resumes_losslessly(
    provider_token: str,
) -> None:
    mailbox = Mailbox()
    first = await invoke(
        mailbox,
        "sync_changes",
        {
            "start_history_id": "90",
            "max_results": 2,
            "page_token": provider_token,
        },
    )
    assert first.is_error is False
    first_value = first.structured_content
    assert first_value is not None
    assert len(first_value["changes"]) == 2
    token = first_value["next_page_token"]
    assert isinstance(token, str) and token != "provider-next"
    second = await invoke(
        mailbox,
        "sync_changes",
        {
            "start_history_id": "90",
            "max_results": 1,
            "page_token": token,
        },
    )
    assert second.is_error is False
    second_value = second.structured_content
    assert second_value is not None and len(second_value["changes"]) == 1
    third = await invoke(
        mailbox,
        "sync_changes",
        {
            "start_history_id": "90",
            "max_results": 2,
            "page_token": second_value["next_page_token"],
        },
    )
    assert third.is_error is False
    third_value = third.structured_content
    assert third_value is not None
    assert [
        item["kind"]
        for page in (first_value, second_value, third_value)
        for item in page["changes"]
    ] == [
        "message_added",
        "message_deleted",
        "labels_added",
        "labels_removed",
    ]
    assert third_value["next_page_token"] == "provider-next"
    history_requests = [
        request for request in mailbox.requests if request.url.path.endswith("/history")
    ]
    assert [request.url.params["pageToken"] for request in history_requests] == [provider_token] * 3
    assert [request.url.params["maxResults"] for request in history_requests] == ["2"] * 3


@pytest.mark.parametrize("mutation", ["start", "account", "malformed"])
async def test_m26_history_overflow_token_is_bound_before_network(mutation: str) -> None:
    mailbox = Mailbox()
    first = await client(mailbox).sync_changes("90", 2)
    cursor = first["next_page_token"]
    mailbox.requests.clear()
    instance = client(mailbox)
    start = "91" if mutation == "start" else "90"
    if mutation == "account":
        instance.credential = replace(instance.credential, account_id="other")
    if mutation == "malformed":
        cursor = "veetbot-history-v1:!invalid!"
    with pytest.raises(GmailError, match=r"gmail\.arguments_invalid"):
        await instance.sync_changes(start, 2, cursor)
    assert mailbox.requests == []


async def test_m26_history_overflow_page_drift_requires_resync_without_partial_replay() -> None:
    class ChangingMailbox(Mailbox):
        changed = False

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            response = await super().__call__(request)
            if request.url.path.endswith("/history") and self.changed:
                body = response.json()
                body["history"][0]["messagesAdded"][0]["message"]["id"] = "different"
                return httpx.Response(200, json=body)
            return response

    mailbox = ChangingMailbox()
    first = await client(mailbox).sync_changes("90", 2)
    mailbox.changed = True
    resumed = await client(mailbox).sync_changes("90", 2, first["next_page_token"])
    assert resumed["resync_required"] is True
    assert resumed["changes"] == [] and resumed["history_id"] is None
    assert resumed["next_page_token"] is None
