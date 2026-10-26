"""Owner-added MCP server routes and the sign-in callback (ADR-0255)."""

from collections.abc import Callable
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_core.application.services import MCPServerSettings
from agent_core.domain.agents import Principal
from agent_core.domain.mcp import (
    MAXIMUM_OWNER_MCP_URL_LENGTH,
    MCPServerAccess,
    MCPServerList,
    MCPServerView,
    MCPSignInStart,
)

_SERVER_ID = Path(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
_PRIVATE = "private, no-store"
_CALLBACK_SCHEME = "veetbot"
_RETURN_PAGE = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width'><title>Veetbot</title></head>"
    "<body><p>You can return to Veetbot.</p></body></html>"
)


class AddMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str = Field(min_length=1, max_length=32)
    url: str = Field(min_length=1, max_length=MAXIMUM_OWNER_MCP_URL_LENGTH)
    access: MCPServerAccess = MCPServerAccess.CHANGES


class UpdateMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access: MCPServerAccess


def mcp_servers_router(service: MCPServerSettings, secured: Callable[[str], object]) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/mcp-servers", openapi_extra={"required_scope": "settings.read"})
    async def list_mcp_servers(
        response: Response,
        authenticated: Annotated[Principal, secured("settings.read")],
    ) -> MCPServerList:
        response.headers["Cache-Control"] = _PRIVATE
        return await service.list(authenticated)

    @router.post(
        "/v1/mcp-servers",
        status_code=201,
        openapi_extra={"required_scope": "settings.write"},
    )
    async def add_mcp_server(
        body: AddMCPServerRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("settings.write")],
    ) -> MCPServerView:
        response.headers["Cache-Control"] = _PRIVATE
        view, created = await service.add(authenticated, body.server_id, body.url, body.access)
        if not created:
            response.status_code = 200
        return view

    @router.patch(
        "/v1/mcp-servers/{server_id}",
        openapi_extra={"required_scope": "settings.write"},
    )
    async def update_mcp_server(
        server_id: Annotated[str, _SERVER_ID],
        body: UpdateMCPServerRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("settings.write")],
    ) -> MCPServerView:
        response.headers["Cache-Control"] = _PRIVATE
        return await service.update_access(authenticated, server_id, body.access)

    @router.delete(
        "/v1/mcp-servers/{server_id}",
        status_code=204,
        openapi_extra={"required_scope": "settings.write"},
    )
    async def remove_mcp_server(
        server_id: Annotated[str, _SERVER_ID],
        authenticated: Annotated[Principal, secured("settings.write")],
    ) -> Response:
        await service.remove(authenticated, server_id)
        return Response(status_code=204, headers={"Cache-Control": _PRIVATE})

    @router.post(
        "/v1/mcp-servers/{server_id}/sign-in",
        openapi_extra={"required_scope": "settings.write"},
    )
    async def begin_mcp_sign_in(
        server_id: Annotated[str, _SERVER_ID],
        response: Response,
        authenticated: Annotated[Principal, secured("settings.write")],
    ) -> MCPSignInStart:
        response.headers["Cache-Control"] = _PRIVATE
        return await service.begin_sign_in(authenticated, server_id)

    @router.delete(
        "/v1/mcp-servers/{server_id}/sign-in",
        status_code=204,
        openapi_extra={"required_scope": "settings.write"},
    )
    async def sign_out_of_mcp_server(
        server_id: Annotated[str, _SERVER_ID],
        authenticated: Annotated[Principal, secured("settings.write")],
    ) -> Response:
        await service.sign_out(authenticated, server_id)
        return Response(status_code=204, headers={"Cache-Control": _PRIVATE})

    @router.get("/v1/mcp-oauth/callback", openapi_extra={"required_scope": None})
    async def complete_mcp_sign_in(
        state: Annotated[str | None, Query(max_length=512)] = None,
        code: Annotated[str | None, Query(max_length=4096)] = None,
        error: Annotated[str | None, Query(max_length=256)] = None,
    ) -> HTMLResponse:
        """The state is the authority; the page reflects nothing from the request."""
        server_id, outcome = await service.complete_sign_in(state, code, error)
        query = (
            {"outcome": outcome}
            if server_id is None
            else {
                "server_id": server_id,
                "outcome": outcome,
            }
        )
        return HTMLResponse(
            _RETURN_PAGE,
            status_code=303,
            headers={
                "Location": f"{_CALLBACK_SCHEME}://mcp-sign-in?{urlencode(query)}",
                "Cache-Control": _PRIVATE,
                "Referrer-Policy": "no-referrer",
            },
        )

    return router
