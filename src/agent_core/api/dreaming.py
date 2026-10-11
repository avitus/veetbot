"""Private reviewed dreaming API and a content-free browser shell."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response
from fastapi.responses import FileResponse

from agent_core.application.services import ReconsolidationService
from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import (
    DreamingDecisionRequest,
    DreamingPauseRequest,
    DreamingSchedule,
    DreamingStatus,
)
from agent_core.domain.memory import Sensitivity
from agent_core.domain.reconsolidation_views import OperationView

_ASSETS = Path(__file__).with_name("dreaming_web")
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}


def dreaming_router(service: ReconsolidationService, secured: Callable[..., object]) -> APIRouter:
    router = APIRouter()

    @router.get("/dreaming", include_in_schema=False)
    async def shell() -> FileResponse:
        return FileResponse(_ASSETS / "index.html", headers=_HEADERS)

    @router.get("/dreaming/app.js", include_in_schema=False)
    async def script() -> FileResponse:
        return FileResponse(_ASSETS / "app.js", media_type="text/javascript", headers=_HEADERS)

    @router.get("/dreaming/style.css", include_in_schema=False)
    async def style() -> FileResponse:
        return FileResponse(_ASSETS / "style.css", media_type="text/css", headers=_HEADERS)

    @router.get("/v1/dreaming", openapi_extra={"required_scope": "memory.read"})
    async def status(
        response: Response, authenticated: Annotated[Principal, secured("memory.read")]
    ) -> DreamingStatus:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.dreaming_status(authenticated)

    @router.post("/v1/dreaming/schedule", openapi_extra={"required_scope": "memory.write"})
    async def pause(
        body: DreamingPauseRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.write")],
    ) -> DreamingSchedule:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.pause_dreaming(
            authenticated, paused=body.paused, expected_revision=body.expected_revision
        )

    @router.post(
        "/v1/dreaming/{operation_id}/decision", openapi_extra={"required_scope": "memory.write"}
    )
    async def decide(
        operation_id: UUID,
        body: DreamingDecisionRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
        ],
    ) -> OperationView:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.decide(
            authenticated,
            operation_id,
            ceiling=ceiling,
            expected_revision=body.expected_revision,
            decision=body.decision,
            key=idempotency_key,
        )

    return router
