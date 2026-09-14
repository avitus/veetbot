"""Mode-confined MCP transport with an immutable operator-reviewed call profile."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from bland_mcp.client import BlandClient, BlandError, identifier

CONFIGURATION_FIELDS = frozenset(
    {
        "version",
        "account_id",
        "phone_number",
        "public_name",
        "public_profile",
        "voice",
        "max_duration_minutes",
        "webhook_url",
    }
)


def configuration_revision(configuration: dict[str, Any]) -> str:
    if set(configuration) != CONFIGURATION_FIELDS or configuration.get("version") != 1:
        raise BlandError("bland.configuration_invalid")
    return hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _error(code: str, *, uncertain: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=code)],
        structured_content={"effect_status": "unknown" if uncertain else "not_applied"},
        is_error=True,
    )


async def _call(operation: Callable[[], Awaitable[dict[str, Any]]]) -> CallToolResult:
    try:
        result = await operation()
    except BlandError as exc:
        return _error(exc.code, uncertain=exc.uncertain)
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, separators=(",", ":")))],
        structured_content=result,
    )


def create_server(mode: str, client: BlandClient, configuration: dict[str, Any]) -> MCPServer:
    if mode not in {"read", "call"}:
        raise BlandError("bland.configuration_invalid")
    revision = configuration_revision(configuration)
    if configuration["phone_number"] != client.number:
        raise BlandError("bland.configuration_invalid")

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await client.close()

    server = MCPServer(f"bland_{mode}", lifespan=lifespan)
    if mode == "read":

        @server.tool()
        async def list_calls(
            limit: Annotated[int, Field(ge=1, le=25)] = 10,
            cursor: str | None = None,
        ) -> CallToolResult:
            """Read retained call summaries in stable call identifier order."""
            return _error("bland.platform_required")

        @server.tool()
        async def get_call(call_id: str) -> CallToolResult:
            """Read a retained Veetbot call transcript and summary using its listed call_id."""
            return _error("bland.platform_required")

        @server.tool(meta={"veetbot/application-only": True})
        async def provider_get_call(provider_call_id: str) -> CallToolResult:
            """Fetch a number-bound provider call for the platform's receipt worker."""
            return await _call(lambda: client.get_call(provider_call_id))

        @server.tool(meta={"veetbot/application-only": True})
        async def provider_list_calls(
            inbound: bool,
            start_date: str,
            offset: Annotated[int, Field(ge=0, le=1_000_000)] = 0,
            limit: Annotated[int, Field(ge=1, le=25)] = 25,
        ) -> CallToolResult:
            """Read one bounded provider page for missed-callback reconciliation."""
            return await _call(
                lambda: client.list_call_ids(
                    inbound=inbound,
                    start_date=start_date,
                    offset=offset,
                    limit=limit,
                )
            )
    else:

        @server.tool(
            description=(
                "Start one AI phone call after owner approval of the recipient, complete brief and "
                "disclosed facts. Generated speech follows the brief; do not promise exact wording "
                "or binding commitments. Caller ID must be " + client.number + "; config_revision "
                "must be " + revision + ". Audio recording is disabled; calls are transcribed. "
                "Maximum duration is " + str(configuration["max_duration_minutes"]) + " minutes. "
                "Accepted does not mean answered. Never redial an uncertain call."
            )
        )
        async def start_call(
            phone_number: Annotated[str, Field(pattern=r"^\+[1-9][0-9]{7,14}$")],
            from_number: Annotated[str, Field(pattern=r"^\+[1-9][0-9]{7,14}$")],
            brief: Annotated[str, Field(min_length=1, max_length=4000)],
            disclosed_facts: Annotated[str, Field(max_length=4000)],
            config_revision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            max_duration_minutes: Annotated[int, Field(ge=1, le=5)] = 5,
            record: bool = False,
            request_id: str | None = None,
        ) -> CallToolResult:
            if (
                config_revision != revision
                or from_number != client.number
                or record
                or max_duration_minutes > configuration["max_duration_minutes"]
            ):
                return _error("bland.configuration_changed")
            try:
                correlation = identifier(request_id)
            except BlandError:
                return _error("bland.platform_required")
            task = (
                "You are an AI assistant making a single call on behalf of "
                + str(configuration["public_name"])
                + ". Introduce yourself as an AI assistant and explain that the call is "
                "transcribed for that person. Pursue only the approved objective below. "
                "Use only the disclosed facts. You have no access to private tools, accounts, "
                "or memories. Do not make purchases, bookings, transfers, or commitments. "
                "If additional authority is needed, take a message and end the call.\n"
                "Approved objective:\n" + brief + "\nPermitted disclosed facts:\n" + disclosed_facts
            )
            return await _call(
                lambda: client.start_call(
                    {
                        "phone_number": phone_number,
                        "from": from_number,
                        "task": task,
                        "max_duration": max_duration_minutes,
                        "record": False,
                        "tools": [],
                        "transfer_list": {},
                        "voice": configuration["voice"],
                        "webhook": configuration["webhook_url"],
                        "metadata": {"veetbot_request_id": correlation},
                    }
                )
            )

        @server.tool(meta={"veetbot/application-only": True})
        async def provider_stop_call(provider_call_id: str) -> CallToolResult:
            """Terminate a verified call only through the platform cancellation lifecycle."""
            return await _call(lambda: client.stop_call(provider_call_id))

    return server
