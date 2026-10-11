"""Flag-mounted owner history and undo; never admits a provider or new text."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response

from agent_core.application.services import ReconsolidationService
from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.reconsolidation_views import (
    OperationKind,
    OperationState,
    OperationView,
    UndoOperationRequest,
)
from agent_core.domain.views import Page


def reconsolidation_router(
    service: ReconsolidationService, secured: Callable[..., object]
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/memory-reconsolidations", openapi_extra={"required_scope": "memory.read"})
    async def list_operations(
        response: Response,
        authenticated: Annotated[Principal, secured("memory.read")],
        ceiling: Sensitivity,
        kind: OperationKind | None = None,
        state: OperationState | None = None,
        limit: Annotated[int, Query(ge=1)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ) -> Page[OperationView]:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.list(
            authenticated,
            ceiling=ceiling,
            kind=kind,
            state=state,
            limit=min(limit, 100),
            cursor=cursor,
        )

    @router.get(
        "/v1/memory-reconsolidations/{operation_id}",
        openapi_extra={"required_scope": "memory.read"},
    )
    async def get_operation(
        operation_id: UUID,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.read")],
        ceiling: Sensitivity,
    ) -> OperationView:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.get(authenticated, operation_id, ceiling=ceiling)

    @router.post(
        "/v1/memory-reconsolidations/{operation_id}/undo",
        openapi_extra={"required_scope": "memory.write"},
    )
    async def undo_operation(
        operation_id: UUID,
        body: UndoOperationRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> OperationView:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.undo(
            authenticated,
            operation_id,
            ceiling=ceiling,
            expected_revision=body.expected_revision,
            key=idempotency_key,
        )

    return router
