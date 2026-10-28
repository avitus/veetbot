"""Private reviewed dreaming API and a content-free browser shell."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent_core.api import dreaming_session
from agent_core.api.boundary import MalformedRequestError
from agent_core.application.errors import SessionMessageCursorError
from agent_core.application.services import ReconsolidationService, SessionService
from agent_core.config import AuthMode, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.dreaming import (
    DreamingDecisionRequest,
    DreamingPauseRequest,
    DreamingSchedule,
    DreamingStatus,
)
from agent_core.domain.errors import NotFoundError
from agent_core.domain.memory import Sensitivity
from agent_core.domain.reconsolidation_views import OperationView
from agent_core.domain.views import Page, SessionMessageView, SessionView
from agent_core.ports.determinism import Clock


class SourceConversationView(BaseModel):
    """An owned conversation page with the operation's cited message identifiers."""

    session: SessionView
    source_event_ids: list[int]
    messages: Page[SessionMessageView]


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


def dreaming_router(
    service: ReconsolidationService,
    secured: Callable[..., object],
    settings: Settings,
    clock: Clock | None,
    sessions: SessionService,
) -> APIRouter:
    if settings.auth_mode is AuthMode.TOKEN and clock is None:
        raise ValueError("review sessions require the composition clock")
    router = APIRouter()

    @router.post("/v1/dreaming/session", openapi_extra={"required_scope": "memory.read"})
    async def connect(
        request: Request,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.read")],
    ) -> dict[str, bool]:
        response.headers["Cache-Control"] = "private, no-store"
        if settings.auth_mode is AuthMode.TOKEN:
            dreaming_session.require_browser(request)
            assert clock is not None
            dreaming_session.issue(response, settings, authenticated, clock)
        return {"connected": True}

    @router.delete("/v1/dreaming/session", openapi_extra={"required_scope": "memory.read"})
    async def disconnect(
        request: Request,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.read")],
    ) -> dict[str, bool]:
        if settings.auth_mode is AuthMode.TOKEN:
            dreaming_session.require_browser(request)
        response.headers["Cache-Control"] = "private, no-store"
        response.delete_cookie(
            dreaming_session.COOKIE, secure=True, httponly=True, samesite="strict", path="/"
        )
        return {"connected": False}

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

    @router.get(
        "/v1/dreaming/{operation_id}/sources/{belief_id}/conversation",
        openapi_extra={"required_scope": "session.read"},
    )
    async def conversation(
        operation_id: UUID,
        belief_id: UUID,
        response: Response,
        authenticated: Annotated[Principal, secured("session.read")],
        ceiling: Sensitivity,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> SourceConversationView:
        """Read only a currently visible operation's owned supporting conversation."""
        response.headers["Cache-Control"] = "private, no-store"
        operation = await service.get(authenticated, operation_id, ceiling=ceiling)
        source = next(
            (s for s in operation.sources if s.belief_id == belief_id and not s.omitted), None
        )
        if source is None or source.session_id is None:
            raise NotFoundError("source conversation unavailable")
        session = await sessions.get(authenticated, source.session_id)
        try:
            messages = await sessions.messages(authenticated, source.session_id, limit, cursor)
        except SessionMessageCursorError as exc:
            raise MalformedRequestError("session message cursor is malformed") from exc
        current = await service.get(authenticated, operation_id, ceiling=ceiling)
        if source not in current.sources:
            raise NotFoundError("source conversation changed")
        return SourceConversationView(
            session=session, source_event_ids=list(source.event_ids), messages=messages
        )

    return router
