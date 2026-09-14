"""Flag-mounted, exact-scope owner call history and termination endpoints."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from agent_core.application.services import CallingService
from agent_core.domain.agents import Principal


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def call_router(service: CallingService, secured: Callable[[str], object]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(private_response)])

    @router.get("/v1/calls", openapi_extra={"required_scope": "call.read"})
    async def calls(
        authenticated: Annotated[Principal, secured("call.read")],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
        cursor: UUID | None = None,
    ) -> dict[str, Any]:
        return await service.list_calls(
            authenticated, limit=limit, cursor=None if cursor is None else str(cursor)
        )

    @router.get("/v1/calls/{call_id}", openapi_extra={"required_scope": "call.read"})
    async def call(
        call_id: UUID, authenticated: Annotated[Principal, secured("call.read")]
    ) -> dict[str, Any]:
        return await service.get_call(authenticated, str(call_id))

    @router.post("/v1/calls/{call_id}/stop", openapi_extra={"required_scope": "call.cancel"})
    async def stop(
        call_id: UUID, authenticated: Annotated[Principal, secured("call.cancel")]
    ) -> dict[str, Any]:
        return await service.stop(authenticated, str(call_id))

    @router.delete("/v1/calls/{call_id}", openapi_extra={"required_scope": "call.delete"})
    async def delete(
        call_id: UUID, authenticated: Annotated[Principal, secured("call.delete")]
    ) -> dict[str, Any]:
        return await service.delete(authenticated, str(call_id))

    return router
