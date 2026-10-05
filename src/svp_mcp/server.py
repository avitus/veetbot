"""Three fixed read tools over the service's published OpenAPI document."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from svp_mcp.client import SvpClient
from svp_mcp.constants import MAXIMUM_LISTED_OPERATIONS, MAXIMUM_RESULT_BYTES
from svp_mcp.errors import SvpError
from svp_mcp.specification import Operation, describe, load_operations, request_target


def _error(code: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=code)],
        structured_content={"effect_status": "not_applied"},
        is_error=True,
    )


async def _call(operation: Callable[[], Awaitable[dict[str, Any]]]) -> CallToolResult:
    try:
        result = await operation()
    except SvpError as exc:
        return _error(exc.code)
    text = json.dumps(result, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAXIMUM_RESULT_BYTES:
        return _error("svp.response_too_large")
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=result)


def create_server(client: SvpClient) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await client.close()

    server = MCPServer("svp_read", lifespan=lifespan)

    async def published(operation_id: str) -> tuple[dict[str, Any], Operation]:
        document = await client.document()
        operation = load_operations(document).get(operation_id)
        if operation is None:
            raise SvpError("svp.operation_unknown")
        return document, operation

    @server.tool()
    async def list_operations() -> CallToolResult:
        """List the read operations of the Scale VP data API with the id each one is called by."""

        async def listed() -> dict[str, Any]:
            operations = load_operations(await client.document())
            rows = [
                {"id": name, "path": operations[name].path, "summary": operations[name].summary}
                for name in sorted(operations)
            ]
            return {
                "operations": rows[:MAXIMUM_LISTED_OPERATIONS],
                "truncated": len(rows) > MAXIMUM_LISTED_OPERATIONS,
            }

        return await _call(listed)

    @server.tool()
    async def describe_operation(operation_id: str) -> CallToolResult:
        """Describe one read operation: its path and query parameters and its response shape."""

        async def described() -> dict[str, Any]:
            document, operation = await published(operation_id)
            return describe(document, operation)

        return await _call(described)

    @server.tool()
    async def call_operation(
        operation_id: str,
        path_parameters: dict[str, str | int] | None = None,
        query: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Run one read operation of the Scale VP data API and return its JSON.

        path_parameters fills the {placeholders} in the operation's path; query may
        name only the query parameters the operation declares.
        """

        async def read() -> dict[str, Any]:
            _document, operation = await published(operation_id)
            url, pairs = request_target(operation, path_parameters or {}, query or {})
            return {"data": await client.get(url, pairs)}

        return await _call(read)

    return server
