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
from svp_mcp.errors import SvpError, SvpRejectionError
from svp_mcp.specification import (
    Operation,
    describe,
    load_operations,
    request_body,
    request_target,
)


def _error(code: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=code)],
        structured_content={"effect_status": "not_applied"},
        is_error=True,
    )


# The platform shows the model no text from a failed call, so anything the model
# can correct, a refused argument or the service's own explanation, is an answer.
_HINTS = {
    "svp.arguments_invalid": (
        "The arguments do not match this operation. Call describe_operation for its path "
        "and query parameters and its request body, and send only those."
    ),
    "svp.operation_unknown": (
        "No read operation has this id. Call list_operations for the ids that exist."
    ),
}


async def _call(operation: Callable[[], Awaitable[dict[str, Any]]]) -> CallToolResult:
    try:
        result = await operation()
    except SvpRejectionError as exc:
        result = {"status": exc.status, "problem": exc.problem}
    except SvpError as exc:
        if exc.code not in _HINTS:
            return _error(exc.code)
        result = {"refused": exc.code, "hint": _HINTS[exc.code]}
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
        """List the read operations of the Scale VP data API (companies, contacts, pipeline,
        deals, investors, markets, news and notes), each with the id it is called by.

        GET operations read by path and query; the POST ones are searches and lookups
        that take a JSON body.
        """

        async def listed() -> dict[str, Any]:
            operations = load_operations(await client.document())
            rows = [
                {
                    "id": name,
                    "method": operations[name].method,
                    "path": operations[name].path,
                    "summary": operations[name].summary,
                }
                for name in sorted(operations)
            ]
            return {
                "operations": rows[:MAXIMUM_LISTED_OPERATIONS],
                "truncated": len(rows) > MAXIMUM_LISTED_OPERATIONS,
            }

        return await _call(listed)

    @server.tool()
    async def describe_operation(operation_id: str) -> CallToolResult:
        """Describe one Scale VP data API read operation by the id list_operations gives:
        its parameters, request body and response shape."""

        async def described() -> dict[str, Any]:
            document, operation = await published(operation_id)
            return describe(document, operation)

        return await _call(described)

    @server.tool()
    async def call_operation(
        operation_id: str,
        path_parameters: dict[str, str | int] | None = None,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Run one Scale VP data API read operation by the id list_operations gives, and
        return its JSON.

        path_parameters fills the {placeholders} in the operation's path; query may
        name only the query parameters the operation declares. body is the JSON
        object of a POST search or lookup and may name only its declared properties;
        a GET operation takes none.
        """

        async def read() -> dict[str, Any]:
            _document, operation = await published(operation_id)
            url, pairs = request_target(operation, path_parameters or {}, query or {})
            payload = request_body(operation, body)
            if payload is None:
                return {"data": await client.get(url, pairs)}
            return {"data": await client.post(url, payload, pairs)}

        return await _call(read)

    return server
