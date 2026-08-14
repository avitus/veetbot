"""FastAPI route table over principal-explicit application services."""

import asyncio
import logging
import unicodedata
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_core.api.auth import Authenticator
from agent_core.api.errors import API_ERROR_STATUS, details_for, mapping_for
from agent_core.api.middleware import PayloadTooLargeError, RequestBoundaryMiddleware
from agent_core.api.sse import encode_sse, heartbeat
from agent_core.application.services import (
    ApprovalService,
    ArtifactService,
    RunService,
    SessionService,
)
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.errors import AgentCoreError
from agent_core.domain.views import (
    ApprovalFilters,
    ApprovalView,
    ArtifactView,
    ContentBlock,
    Page,
    RunView,
    SessionView,
    StreamFrame,
    SubmitResult,
)

logger = logging.getLogger(__name__)
IDEMPOTENCY_KEY_MAX_LENGTH = 255
APPROVAL_REASON_MAX_LENGTH = 4096


class MalformedRequestError(ValueError):
    """A syntactically invalid value detected at the HTTP boundary."""


def _content_disposition(filename: str) -> str:
    safe_name = "".join(
        "_" if character in '/\\"' or unicodedata.category(character) == "Cc" else character
        for character in filename
    ).strip()
    safe_name = safe_name or "artifact"
    fallback = unicodedata.normalize("NFKD", safe_name).encode("ascii", "ignore").decode()
    fallback = "".join(
        "_" if character in '/\\"' or unicodedata.category(character) == "Cc" else character
        for character in fallback
    ).strip()
    fallback = fallback or "artifact"
    encoded = quote(safe_name, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def _matches_etag(header: str | None, sha256: str) -> bool:
    if header is None:
        return False
    for raw in header.split(","):
        candidate = raw.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == "*" or candidate.strip('"') == sha256:
            return True
    return False


class ApplicationServices(Protocol):
    @property
    def sessions(self) -> SessionService: ...

    @property
    def runs(self) -> RunService: ...

    @property
    def approvals(self) -> ApprovalService: ...

    @property
    def artifacts(self) -> ArtifactService: ...


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: list[ContentBlock] = Field(min_length=1)


class InputRequest(MessageRequest):
    question_id: UUID | None = None


class ResolveApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalResolutionType
    reason: str | None = Field(default=None, max_length=APPROVAL_REASON_MAX_LENGTH)


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


def _error_response(
    request: Request,
    *,
    code: str,
    status: int,
    message: str,
    details: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": _request_id(request),
            }
        },
        headers=headers,
    )


def create_app(
    services: ApplicationServices,
    settings: Settings,
    principal: Principal,
    new_request_id: Callable[[], str],
    readiness_probe: Callable[[], Awaitable[bool]],
) -> FastAPI:
    app = FastAPI(
        title="Agent Core API",
        version="0.1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    auth = Authenticator(settings, principal)
    app.add_middleware(
        RequestBoundaryMiddleware,
        new_request_id=new_request_id,
        early_authenticate=auth.authenticate_scope,
    )

    @app.exception_handler(AgentCoreError)
    async def domain_error(request: Request, exc: AgentCoreError) -> JSONResponse:
        mapping = mapping_for(exc)
        headers = {"WWW-Authenticate": "Bearer"} if mapping.status == 401 else None
        return _error_response(
            request,
            code=mapping.code,
            status=mapping.status,
            message=str(exc) or "The request could not be completed.",
            details=details_for(exc, mapping.code),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        del exc
        return _error_response(
            request,
            code="malformed_request",
            status=API_ERROR_STATUS["malformed_request"],
            message="The request body or parameters are malformed.",
        )

    @app.exception_handler(MalformedRequestError)
    async def malformed_request_error(request: Request, exc: MalformedRequestError) -> JSONResponse:
        return _error_response(
            request,
            code="malformed_request",
            status=API_ERROR_STATUS["malformed_request"],
            message=str(exc) or "The request is malformed.",
        )

    @app.exception_handler(PayloadTooLargeError)
    async def payload_too_large(request: Request, exc: PayloadTooLargeError) -> JSONResponse:
        del exc
        return _error_response(
            request,
            code="payload_too_large",
            status=API_ERROR_STATUS["payload_too_large"],
            message="The request body exceeds the 1 MiB limit.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "malformed_request"
        status = 404 if exc.status_code == 404 else 400
        return _error_response(
            request,
            code=code,
            status=status,
            message="The requested resource was not found."
            if status == 404
            else "The HTTP request is not supported.",
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "api_request_failed",
            extra={"request_id": _request_id(request), "error_class": type(exc).__name__},
        )
        return _error_response(
            request,
            code="internal_error",
            status=500,
            message="An internal error prevented the request from completing.",
        )

    def secured(scope: str) -> object:
        return Depends(auth.require(scope))

    @app.post(
        "/v1/sessions",
        status_code=201,
        openapi_extra={"required_scope": "session.write"},
    )
    async def create_session(
        body: CreateSessionRequest,
        authenticated: Annotated[Principal, secured("session.write")],
    ) -> SessionView:
        return await services.sessions.create(authenticated, body.agent_id, body.metadata)

    @app.get(
        "/v1/sessions",
        openapi_extra={"required_scope": "session.read"},
    )
    async def list_sessions(
        authenticated: Annotated[Principal, secured("session.read")],
        limit: Annotated[int, Query(ge=1)] = 50,
        cursor: str | None = None,
    ) -> Page[SessionView]:
        try:
            return await services.sessions.list(authenticated, limit, cursor)
        except ValueError as exc:
            raise MalformedRequestError("session cursor is malformed") from exc

    @app.get(
        "/v1/sessions/{session_id}",
        openapi_extra={"required_scope": "session.read"},
    )
    async def get_session(
        session_id: UUID,
        authenticated: Annotated[Principal, secured("session.read")],
    ) -> SessionView:
        return await services.sessions.get(authenticated, session_id)

    @app.delete(
        "/v1/sessions/{session_id}",
        status_code=204,
        openapi_extra={"required_scope": "session.write"},
    )
    async def delete_session(
        session_id: UUID,
        authenticated: Annotated[Principal, secured("session.write")],
    ) -> Response:
        await services.sessions.delete(authenticated, session_id)
        return Response(status_code=204)

    @app.post(
        "/v1/sessions/{session_id}/messages",
        openapi_extra={"required_scope": "run.write"},
    )
    async def submit_message(
        session_id: UUID,
        body: MessageRequest,
        authenticated: Annotated[Principal, secured("run.write")],
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=IDEMPOTENCY_KEY_MAX_LENGTH),
        ] = None,
    ) -> Response:
        result = await services.runs.submit(
            authenticated,
            session_id,
            body.content,
            idempotency_key,
            None,
        )
        return JSONResponse(
            status_code=200 if result.replayed else 202,
            content=result.model_dump(mode="json"),
        )

    @app.get(
        "/v1/runs/{run_id}",
        openapi_extra={"required_scope": "run.read"},
    )
    async def get_run(
        run_id: UUID,
        authenticated: Annotated[Principal, secured("run.read")],
    ) -> RunView:
        return await services.runs.get(authenticated, run_id)

    @app.get(
        "/v1/runs/{run_id}/events",
        openapi_extra={"required_scope": "run.read"},
    )
    async def stream_events(
        run_id: UUID,
        authenticated: Annotated[Principal, secured("run.read")],
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        try:
            after = None if last_event_id is None else int(last_event_id)
        except ValueError as exc:
            raise MalformedRequestError("Last-Event-ID must be an integer") from exc
        if after is not None and after < 0:
            raise MalformedRequestError("Last-Event-ID must not be negative")
        # Complete all checks before StreamingResponse sends its first byte.
        await services.runs.get(authenticated, run_id)

        async def frames() -> AsyncIterator[bytes]:
            iterator = cast(
                AsyncGenerator[StreamFrame, None],
                services.runs.stream(authenticated, run_id, after).__aiter__(),
            )
            pending: asyncio.Task[StreamFrame] | None = None

            async def next_frame() -> StreamFrame:
                return await iterator.__anext__()

            try:
                while True:
                    if pending is None:
                        pending = asyncio.create_task(next_frame())
                    done, _ = await asyncio.wait({pending}, timeout=15.0)
                    if not done:
                        yield heartbeat()
                        continue
                    try:
                        frame = pending.result()
                    except StopAsyncIteration:
                        return
                    pending = None
                    yield encode_sse(frame)
            finally:
                if pending is not None:
                    pending.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await pending
                await iterator.aclose()

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/v1/runs/{run_id}/cancel",
        openapi_extra={"required_scope": "run.cancel"},
    )
    async def cancel_run(
        run_id: UUID,
        authenticated: Annotated[Principal, secured("run.cancel")],
    ) -> Response:
        result = await services.runs.cancel(authenticated, run_id)
        return JSONResponse(
            status_code=202 if result.accepted else 200,
            content=result.run.model_dump(mode="json"),
        )

    @app.post(
        "/v1/runs/{run_id}/input",
        status_code=202,
        openapi_extra={"required_scope": "run.write"},
    )
    async def deliver_input(
        run_id: UUID,
        body: InputRequest,
        authenticated: Annotated[Principal, secured("run.write")],
    ) -> SubmitResult:
        return await services.runs.deliver_input(
            authenticated, run_id, body.content, body.question_id
        )

    @app.get(
        "/v1/approvals",
        openapi_extra={"required_scope": "approval.read"},
    )
    async def list_approvals(
        authenticated: Annotated[Principal, secured("approval.read")],
        status: Literal["pending"] = "pending",
        run_id: UUID | None = None,
        session_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1)] = 50,
        cursor: str | None = None,
    ) -> Page[ApprovalView]:
        return await services.approvals.list(
            authenticated,
            ApprovalFilters(status=status, run_id=run_id, session_id=session_id),
            limit,
            cursor,
        )

    @app.get(
        "/v1/approvals/{approval_id}",
        openapi_extra={"required_scope": "approval.read"},
    )
    async def get_approval(
        approval_id: UUID,
        authenticated: Annotated[Principal, secured("approval.read")],
    ) -> ApprovalView:
        return await services.approvals.get(authenticated, approval_id)

    @app.post(
        "/v1/approvals/{approval_id}/resolve",
        openapi_extra={"required_scope": "approval.resolve"},
    )
    async def resolve_approval(
        approval_id: UUID,
        body: ResolveApprovalRequest,
        authenticated: Annotated[Principal, secured("approval.resolve")],
    ) -> ApprovalView:
        return await services.approvals.resolve(
            authenticated, approval_id, body.decision, body.reason
        )

    @app.get(
        "/v1/artifacts/{artifact_id}",
        openapi_extra={"required_scope": "artifact.read"},
    )
    async def get_artifact(
        artifact_id: UUID,
        authenticated: Annotated[Principal, secured("artifact.read")],
    ) -> ArtifactView:
        return await services.artifacts.get(authenticated, artifact_id)

    @app.get(
        "/v1/artifacts/{artifact_id}/content",
        openapi_extra={"required_scope": "artifact.read"},
    )
    async def get_artifact_content(
        artifact_id: UUID,
        authenticated: Annotated[Principal, secured("artifact.read")],
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    ) -> Response:
        content = await services.artifacts.open_content(authenticated, artifact_id)
        artifact = content.artifact
        private_cache_headers = {
            "Cache-Control": "private, no-store",
            "ETag": f'"{artifact.sha256}"',
        }
        if _matches_etag(if_none_match, artifact.sha256):
            return Response(status_code=304, headers=private_cache_headers)
        stream = await content.open()
        return StreamingResponse(
            stream,
            media_type=artifact.media_type,
            headers={
                "Content-Length": str(artifact.size_bytes),
                "Content-Disposition": _content_disposition(artifact.name),
                **private_cache_headers,
            },
        )

    @app.get("/health/live", openapi_extra={"required_scope": None})
    async def health_live(response: Response) -> dict[str, str]:
        if settings.release_id is not None:
            response.headers["X-Veetbot-Release"] = settings.release_id
        return {"status": "ok"}

    @app.get("/health/ready", openapi_extra={"required_scope": None})
    async def health_ready() -> Response:
        ready = await readiness_probe()
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
            headers=(
                {"X-Veetbot-Release": settings.release_id}
                if settings.release_id is not None
                else None
            ),
        )

    return app
