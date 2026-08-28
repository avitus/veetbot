"""Mode-confined official-SDK MCP servers."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from gmail_mcp.client import GmailClient
from gmail_mcp.constants import ROSTERS
from gmail_mcp.errors import GmailError


async def _call(
    operation: Callable[..., Awaitable[dict[str, Any]]],
    *args: object,
) -> CallToolResult:
    try:
        result = await operation(*args)
    except GmailError as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=exc.code)],
            structured_content={
                "effect_status": (
                    "unknown" if exc.code == "gmail.outcome_unknown" else "not_applied"
                )
            },
            is_error=True,
        )
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
        ],
        structured_content=result,
        is_error=False,
    )


def create_server(mode: str, client: GmailClient) -> MCPServer:
    """Create exactly one of the three honest server-level rosters."""

    if mode not in ROSTERS:
        raise ValueError("Gmail MCP mode must be read, write, or send")

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        await client.authenticate()
        try:
            yield None
        finally:
            await client.close()

    server = MCPServer(f"gmail_{mode}", lifespan=lifespan)

    if mode == "read":

        @server.tool()
        async def search_threads(
            query: str,
            max_results: int,
            page_token: str | None = None,
        ) -> CallToolResult:
            """Search Gmail threads using Gmail query syntax."""

            return await _call(client.search_threads, query, max_results, page_token)

        @server.tool()
        async def get_thread(thread_id: str) -> CallToolResult:
            """Read every message in one thread without fetching attachments."""

            return await _call(client.get_thread, thread_id)

        @server.tool()
        async def list_labels() -> CallToolResult:
            """List system and user Gmail labels."""

            return await _call(client.list_labels)

    elif mode == "write":

        @server.tool()
        async def create_draft(
            to: str,
            subject: str,
            body: str,
            cc: str | None = None,
            bcc: str | None = None,
            thread_id: str | None = None,
        ) -> CallToolResult:
            """Create a plain-text draft, optionally in an existing thread."""

            return await _call(client.create_draft, to, subject, body, cc, bcc, thread_id)

        @server.tool()
        async def modify_labels(
            thread_ids: list[str],
            add_label_ids: list[str] | None = None,
            remove_label_ids: list[str] | None = None,
        ) -> CallToolResult:
            """Add or remove labels on at most twenty-five threads."""

            return await _call(
                client.modify_labels,
                thread_ids,
                add_label_ids,
                remove_label_ids,
            )

        @server.tool()
        async def trash_thread(thread_id: str) -> CallToolResult:
            """Move a thread to Gmail's reversible trash."""

            return await _call(client.trash_thread, thread_id)

        @server.tool()
        async def untrash_thread(thread_id: str) -> CallToolResult:
            """Restore a thread from Gmail's reversible trash."""

            return await _call(client.untrash_thread, thread_id)

    else:

        @server.tool()
        async def send_message(
            to: str,
            subject: str,
            body: str,
            cc: str | None = None,
            bcc: str | None = None,
            thread_id: str | None = None,
        ) -> CallToolResult:
            """Send one plain-text message by value, optionally as a reply."""

            return await _call(client.send_message, to, subject, body, cc, bcc, thread_id)

    return server
