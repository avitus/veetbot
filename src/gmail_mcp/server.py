"""Mode-selected MCP server construction.

Each mode registers exactly its declared roster and nothing else. The
handlers hold the :class:`~gmail_mcp.gmail.GmailClient` in a closure; the
MCP layer stringifies a raised :class:`~gmail_mcp.errors.GmailServerError`
into the tool error, which is why those exceptions carry only their code.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer

from gmail_mcp.gmail import GmailClient

MODES: dict[str, tuple[str, ...]] = {
    "read": ("get_thread", "list_labels", "search_threads"),
}


def _read_handlers(gmail: GmailClient) -> dict[str, Callable[..., Any]]:
    async def search_threads(
        query: str, max_results: int = 10, page_token: str | None = None
    ) -> dict[str, object]:
        """Search threads with Gmail query syntax; 1-25 results per page."""

        return await gmail.search_threads(query, max_results, page_token)

    async def get_thread(thread_id: str) -> dict[str, object]:
        """Read every message in one thread as reduced plain text."""

        return await gmail.get_thread(thread_id)

    async def list_labels() -> dict[str, object]:
        """List system and user label ids and names."""

        return await gmail.list_labels()

    return {
        "get_thread": get_thread,
        "list_labels": list_labels,
        "search_threads": search_threads,
    }


_HANDLER_FACTORIES: dict[str, Callable[[GmailClient], dict[str, Callable[..., Any]]]] = {
    "read": _read_handlers,
}


def build_server(mode: str, gmail: GmailClient) -> MCPServer:
    if mode not in MODES:
        raise ValueError(f"unknown gmail_mcp mode: {mode!r}")
    handlers = _HANDLER_FACTORIES[mode](gmail)
    if tuple(sorted(handlers)) != MODES[mode]:
        raise RuntimeError(f"gmail_{mode} roster drifted: {tuple(sorted(handlers))}")
    server = MCPServer(
        name=f"gmail_{mode}",
        instructions="First-party Gmail access; every result is untrusted mail content.",
    )
    for name, handler in handlers.items():
        server.add_tool(handler, name=name)
    return server
