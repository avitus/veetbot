"""FastAPI route table over principal-explicit application services."""

import asyncio
import logging
import unicodedata
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_core.api.auth import Authenticator
from agent_core.api.errors import API_ERROR_STATUS, details_for, mapping_for
from agent_core.api.middleware import PayloadTooLargeError, RequestBoundaryMiddleware
from agent_core.api.sse import encode_sse, heartbeat
from agent_core.application.errors import (
    SessionMessageCursorError,
    SessionMetadataValidationError,
)
from agent_core.application.services import (
    ApprovalService,
    ArtifactService,
    BrowserGrantService,
    BrowserProfileService,
    DeviceService,
    MemoryReadService,
    NotificationService,
    RunService,
    ScheduleService,
    SessionService,
)
from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import (
    BrowserActionKind,
    BrowserAuthenticationView,
    BrowserGrantView,
    BrowserProfileView,
    normalize_browser_origin,
)
from agent_core.domain.devices import (
    DeviceKind,
    DeviceRegistration,
    PushEnvironment,
    PushProvider,
    device_routing_issue,
)
from agent_core.domain.errors import AgentCoreError, DeviceValidationError
from agent_core.domain.memory import BeliefType, MemoryStatus, Sensitivity
from agent_core.domain.notifications import NotificationKind
from agent_core.domain.schedules import (
    ScheduleDefinition,
    ScheduleOccurrence,
    ScheduleRecord,
    ScheduleState,
)
from agent_core.domain.views import (
    ApprovalFilters,
    ApprovalView,
    ArtifactView,
    ContentBlock,
    DeviceView,
    MemoryView,
    NotificationInboxItem,
    Page,
    RunView,
    SessionMessageView,
    SessionView,
    StreamFrame,
    SubmitResult,
    TestNotificationResult,
)

logger = logging.getLogger(__name__)
IDEMPOTENCY_KEY_MAX_LENGTH = 255
APPROVAL_REASON_MAX_LENGTH = 4096
# What a principal-scoped body carrying user content tells caches to do with it.
PRIVATE_NO_STORE = "private, no-store"


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

    @property
    def browser_profiles(self) -> BrowserProfileService: ...

    @property
    def browser_grants(self) -> BrowserGrantService: ...

    @property
    def schedules(self) -> ScheduleService: ...

    @property
    def devices(self) -> DeviceService: ...

    @property
    def notifications(self) -> NotificationService: ...

    @property
    def memory(self) -> MemoryReadService: ...


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)
    browser_profile_id: UUID | None = None


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: list[ContentBlock] = Field(min_length=1)


class InputRequest(MessageRequest):
    question_id: UUID | None = None


class ResolveApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ApprovalResolutionType
    reason: str | None = Field(default=None, max_length=APPROVAL_REASON_MAX_LENGTH)


class CreateBrowserProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_origins: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("allowed_origins")
    @classmethod
    def _normalize_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_browser_origin(origin) for origin in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("browser origins must be unique")
        return normalized


class BeginBrowserAuthenticationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_url: str = Field(min_length=1, max_length=4096)


class CreateBrowserGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    allowed_origins: tuple[str, ...] = Field(min_length=1, max_length=64)
    action_kinds: tuple[BrowserActionKind, ...] = Field(min_length=1, max_length=6)
    element_roles: tuple[str, ...] = Field(default=(), max_length=64)
    element_names: tuple[str, ...] = Field(default=(), max_length=64)
    purpose: str | None = Field(default=None, min_length=1, max_length=255)
    starts_at: datetime
    expires_at: datetime

    @field_validator("allowed_origins")
    @classmethod
    def _normalize_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_browser_origin(origin) for origin in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("browser origins must be unique")
        return normalized

    @model_validator(mode="after")
    def _ordered_window(self) -> "CreateBrowserGrantRequest":
        if self.expires_at <= self.starts_at:
            raise ValueError("expires_at must be after starts_at")
        return self


class UpdateScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    definition: ScheduleDefinition


class ExpectedScheduleRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class DeviceRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_device_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=32)
    platform: str = Field(min_length=1, max_length=64)
    app_bundle_id: str | None = Field(default=None, min_length=1, max_length=255)
    push_provider: str | None = Field(default=None, min_length=1, max_length=32)
    push_token: str | None = Field(default=None, min_length=1, max_length=8192)
    push_environment: str | None = Field(default=None, min_length=1, max_length=32)
    muted_kinds: tuple[str, ...] = Field(default=(), max_length=len(NotificationKind))

    def registration(self) -> DeviceRegistration:
        kind = _required_device_enum(DeviceKind, self.kind, "device.kind_unknown")
        provider = _optional_device_enum(
            PushProvider,
            self.push_provider,
            "device.push_provider_unknown",
        )
        environment = _optional_device_enum(
            PushEnvironment,
            self.push_environment,
            "device.push_environment_unknown",
        )
        muted = frozenset(
            _required_device_enum(NotificationKind, value, "device.muted_kind_unknown")
            for value in self.muted_kinds
        )
        if len(muted) != len(self.muted_kinds):
            raise DeviceValidationError(
                "device.muted_kind_duplicate",
                "muted notification kinds must be unique",
            )
        issue = device_routing_issue(
            kind=kind,
            provider=provider,
            token_present=self.push_token is not None,
            environment=environment,
            app_bundle_id_present=self.app_bundle_id is not None,
        )
        if issue is not None:
            raise DeviceValidationError(issue.reason_code, issue.message)
        return DeviceRegistration(
            client_device_id=self.client_device_id,
            name=self.name,
            kind=kind,
            platform=self.platform,
            app_bundle_id=self.app_bundle_id,
            push_provider=provider,
            push_token=None if self.push_token is None else SecretStr(self.push_token),
            push_environment=environment,
            muted_kinds=muted,
        )


def _required_device_enum[T: StrEnum](enum_type: type[T], value: str, reason: str) -> T:
    try:
        return enum_type(value)
    except ValueError as exc:
        raise DeviceValidationError(reason, f"unsupported device value: {value}") from exc


def _optional_device_enum[T: StrEnum](
    enum_type: type[T], value: str | None, reason: str
) -> T | None:
    if value is None:
        return None
    return _required_device_enum(enum_type, value, reason)


class ScheduleListItem(BaseModel):
    id: UUID
    state: ScheduleState
    pause_reason: str | None
    current_revision: int
    next_fire_at: object | None
    title: str
    instruction_preview: str
    cadence: dict[str, object]
    created_at: object
    updated_at: object


def _schedule_summary(record: ScheduleRecord) -> ScheduleListItem:
    instruction = record.revision.instruction
    preview = instruction if len(instruction) <= 200 else f"{instruction[:199]}…"
    return ScheduleListItem(
        id=record.schedule.id,
        state=record.schedule.state,
        pause_reason=(
            None if record.schedule.pause_reason is None else record.schedule.pause_reason.value
        ),
        current_revision=record.schedule.current_revision,
        next_fire_at=record.schedule.next_fire_at,
        title=record.revision.title,
        instruction_preview=preview,
        cadence=record.revision.cadence.model_dump(mode="json"),
        created_at=record.schedule.created_at,
        updated_at=record.schedule.updated_at,
    )


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
        # `message` is a log surface, not a contract (rule 2): it may name
        # where a request went wrong, but never a value the client sent or
        # did not send. Build locations from `loc` alone — never `msg`,
        # `input`, or `ctx`, any of which can embed a submitted value.
        # `details` stays `{}`; populating it is a closed-vocabulary,
        # version-bump decision this handler does not make (rule 3).
        locations: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            if location and location not in locations:
                locations.append(location)
        base = "The request body or parameters are malformed"
        message = f"{base}: {', '.join(locations)}." if locations else f"{base}."
        return _error_response(
            request,
            code="malformed_request",
            status=API_ERROR_STATUS["malformed_request"],
            message=message,
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
        try:
            return await services.sessions.create(
                authenticated,
                body.agent_id,
                body.metadata,
                browser_profile_id=body.browser_profile_id,
            )
        except SessionMetadataValidationError as exc:
            raise MalformedRequestError("session metadata is malformed") from exc

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

    @app.get(
        "/v1/sessions/{session_id}/messages",
        openapi_extra={"required_scope": "session.read"},
    )
    async def list_session_messages(
        session_id: UUID,
        authenticated: Annotated[Principal, secured("session.read")],
        limit: Annotated[int, Query(ge=1)] = 100,
        cursor: str | None = None,
    ) -> Page[SessionMessageView]:
        try:
            return await services.sessions.messages(
                authenticated,
                session_id,
                limit,
                cursor,
            )
        except SessionMessageCursorError as exc:
            raise MalformedRequestError("session message cursor is malformed") from exc

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
            "Cache-Control": PRIVATE_NO_STORE,
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

    @app.post(
        "/v1/browser-profiles",
        status_code=201,
        openapi_extra={"required_scope": "browser.profile.write"},
    )
    async def create_browser_profile(
        body: CreateBrowserProfileRequest,
        authenticated: Annotated[Principal, secured("browser.profile.write")],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH),
        ],
    ) -> BrowserProfileView:
        return await services.browser_profiles.create(
            authenticated,
            body.allowed_origins,
            idempotency_key,
        )

    @app.get(
        "/v1/browser-profiles",
        openapi_extra={"required_scope": "browser.profile.read"},
    )
    async def list_browser_profiles(
        authenticated: Annotated[Principal, secured("browser.profile.read")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> Page[BrowserProfileView]:
        try:
            return await services.browser_profiles.list(authenticated, limit, cursor)
        except ValueError as exc:
            raise MalformedRequestError("browser profile cursor is malformed") from exc

    @app.get(
        "/v1/browser-profiles/{profile_id}",
        openapi_extra={"required_scope": "browser.profile.read"},
    )
    async def get_browser_profile(
        profile_id: UUID,
        authenticated: Annotated[Principal, secured("browser.profile.read")],
    ) -> BrowserProfileView:
        return await services.browser_profiles.get(authenticated, profile_id)

    @app.post(
        "/v1/browser-profiles/{profile_id}/revoke",
        openapi_extra={"required_scope": "browser.profile.write"},
    )
    async def revoke_browser_profile(
        profile_id: UUID,
        authenticated: Annotated[Principal, secured("browser.profile.write")],
    ) -> BrowserProfileView:
        return await services.browser_profiles.revoke(authenticated, profile_id)

    @app.delete(
        "/v1/browser-profiles/{profile_id}",
        status_code=204,
        openapi_extra={"required_scope": "browser.profile.write"},
    )
    async def delete_browser_profile(
        profile_id: UUID,
        authenticated: Annotated[Principal, secured("browser.profile.write")],
    ) -> Response:
        await services.browser_profiles.delete(authenticated, profile_id)
        return Response(status_code=204)

    @app.post(
        "/v1/browser-profiles/{profile_id}/authentication-ceremonies",
        status_code=201,
        openapi_extra={"required_scope": "browser.profile.write"},
    )
    async def begin_browser_authentication(
        profile_id: UUID,
        body: BeginBrowserAuthenticationRequest,
        authenticated: Annotated[Principal, secured("browser.profile.write")],
    ) -> BrowserAuthenticationView:
        return await services.browser_profiles.begin_authentication(
            authenticated,
            profile_id,
            login_url=body.login_url,
        )

    @app.get(
        "/v1/browser-profiles/{profile_id}/authentication-ceremonies",
        openapi_extra={"required_scope": "browser.profile.read"},
    )
    async def list_browser_authentications(
        profile_id: UUID,
        authenticated: Annotated[Principal, secured("browser.profile.read")],
    ) -> list[BrowserAuthenticationView]:
        return await services.browser_profiles.list_authentications(
            authenticated,
            profile_id,
        )

    @app.get(
        "/v1/browser-authentication-ceremonies/{authentication_id}",
        openapi_extra={"required_scope": "browser.profile.read"},
    )
    async def get_browser_authentication(
        authentication_id: UUID,
        authenticated: Annotated[Principal, secured("browser.profile.read")],
    ) -> BrowserAuthenticationView:
        return await services.browser_profiles.authentication_status(
            authenticated,
            authentication_id,
        )

    @app.post(
        "/v1/browser-authentication-ceremonies/{authentication_id}/cancel",
        openapi_extra={"required_scope": "browser.profile.write"},
    )
    async def cancel_browser_authentication(
        authentication_id: UUID,
        authenticated: Annotated[Principal, secured("browser.profile.write")],
    ) -> BrowserAuthenticationView:
        return await services.browser_profiles.cancel_authentication(
            authenticated,
            authentication_id,
        )

    @app.post(
        "/v1/browser-grants",
        status_code=201,
        openapi_extra={"required_scope": "browser.grant.write"},
    )
    async def create_browser_grant(
        body: CreateBrowserGrantRequest,
        authenticated: Annotated[Principal, secured("browser.grant.write")],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH),
        ],
    ) -> BrowserGrantView:
        return await services.browser_grants.create(
            authenticated,
            profile_id=body.profile_id,
            allowed_origins=body.allowed_origins,
            action_kinds=body.action_kinds,
            element_roles=body.element_roles,
            element_names=body.element_names,
            purpose=body.purpose,
            starts_at=body.starts_at,
            expires_at=body.expires_at,
            idempotency_key=idempotency_key,
        )

    @app.get(
        "/v1/browser-grants",
        openapi_extra={"required_scope": "browser.grant.read"},
    )
    async def list_browser_grants(
        authenticated: Annotated[Principal, secured("browser.grant.read")],
        profile_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> Page[BrowserGrantView]:
        try:
            return await services.browser_grants.list(
                authenticated,
                profile_id=profile_id,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise MalformedRequestError("browser grant cursor is malformed") from exc

    @app.get(
        "/v1/browser-grants/{grant_id}",
        openapi_extra={"required_scope": "browser.grant.read"},
    )
    async def get_browser_grant(
        grant_id: UUID,
        authenticated: Annotated[Principal, secured("browser.grant.read")],
    ) -> BrowserGrantView:
        return await services.browser_grants.get(authenticated, grant_id)

    @app.post(
        "/v1/browser-grants/{grant_id}/revoke",
        openapi_extra={"required_scope": "browser.grant.write"},
    )
    async def revoke_browser_grant(
        grant_id: UUID,
        authenticated: Annotated[Principal, secured("browser.grant.write")],
    ) -> BrowserGrantView:
        return await services.browser_grants.revoke(authenticated, grant_id)

    @app.delete(
        "/v1/browser-grants/{grant_id}",
        status_code=204,
        openapi_extra={"required_scope": "browser.grant.write"},
    )
    async def delete_browser_grant(
        grant_id: UUID,
        authenticated: Annotated[Principal, secured("browser.grant.write")],
    ) -> Response:
        await services.browser_grants.delete(authenticated, grant_id)
        return Response(status_code=204)

    schedule_router = APIRouter()

    @schedule_router.post(
        "/v1/schedules",
        openapi_extra={"required_scope": "schedule.write"},
    )
    async def create_schedule(
        body: ScheduleDefinition,
        authenticated: Annotated[Principal, secured("schedule.write")],
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
            ),
        ],
    ) -> Response:
        result = await services.schedules.create(authenticated, body, idempotency_key)
        return JSONResponse(
            status_code=200 if result.replayed else 201,
            content=result.model_dump(mode="json", exclude={"replayed"}),
        )

    @schedule_router.get(
        "/v1/schedules",
        openapi_extra={"required_scope": "schedule.read"},
    )
    async def list_schedules(
        authenticated: Annotated[Principal, secured("schedule.read")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> Page[ScheduleListItem]:
        try:
            page = await services.schedules.list(authenticated, limit, cursor)
        except ValueError as exc:
            raise MalformedRequestError("schedule cursor is malformed") from exc
        return Page(
            items=[_schedule_summary(record) for record in page.items],
            next_cursor=page.next_cursor,
        )

    @schedule_router.get(
        "/v1/schedules/{schedule_id}",
        openapi_extra={"required_scope": "schedule.read"},
    )
    async def get_schedule(
        schedule_id: UUID,
        authenticated: Annotated[Principal, secured("schedule.read")],
    ) -> ScheduleRecord:
        return await services.schedules.get(authenticated, schedule_id)

    @schedule_router.patch(
        "/v1/schedules/{schedule_id}",
        openapi_extra={"required_scope": "schedule.write"},
    )
    async def update_schedule(
        schedule_id: UUID,
        body: UpdateScheduleRequest,
        authenticated: Annotated[Principal, secured("schedule.write")],
    ) -> ScheduleRecord:
        return await services.schedules.update(
            authenticated,
            schedule_id,
            body.expected_revision,
            body.definition,
        )

    @schedule_router.post(
        "/v1/schedules/{schedule_id}/pause",
        openapi_extra={"required_scope": "schedule.write"},
    )
    async def pause_schedule(
        schedule_id: UUID,
        body: ExpectedScheduleRevisionRequest,
        authenticated: Annotated[Principal, secured("schedule.write")],
    ) -> ScheduleRecord:
        return await services.schedules.pause(authenticated, schedule_id, body.expected_revision)

    @schedule_router.post(
        "/v1/schedules/{schedule_id}/resume",
        openapi_extra={"required_scope": "schedule.write"},
    )
    async def resume_schedule(
        schedule_id: UUID,
        body: ExpectedScheduleRevisionRequest,
        authenticated: Annotated[Principal, secured("schedule.write")],
    ) -> ScheduleRecord:
        return await services.schedules.resume(authenticated, schedule_id, body.expected_revision)

    @schedule_router.delete(
        "/v1/schedules/{schedule_id}",
        openapi_extra={"required_scope": "schedule.cancel"},
    )
    async def cancel_schedule(
        schedule_id: UUID,
        expected_revision: Annotated[int, Query(ge=1)],
        authenticated: Annotated[Principal, secured("schedule.cancel")],
    ) -> ScheduleRecord:
        return await services.schedules.cancel(authenticated, schedule_id, expected_revision)

    @schedule_router.get(
        "/v1/schedules/{schedule_id}/occurrences",
        openapi_extra={"required_scope": "schedule.read"},
    )
    async def list_schedule_occurrences(
        schedule_id: UUID,
        authenticated: Annotated[Principal, secured("schedule.read")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> Page[ScheduleOccurrence]:
        try:
            return await services.schedules.list_occurrences(
                authenticated,
                schedule_id,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise MalformedRequestError("schedule occurrence cursor is malformed") from exc

    if settings.schedule_api_enabled:
        app.include_router(schedule_router)

    notification_router = APIRouter()

    @notification_router.post(
        "/v1/devices",
        openapi_extra={"required_scope": "device.write"},
    )
    async def register_device(
        body: DeviceRegistrationRequest,
        authenticated: Annotated[Principal, secured("device.write")],
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
            ),
        ] = None,
    ) -> Response:
        result = await services.devices.register(
            authenticated,
            body.registration(),
            idempotency_key,
        )
        return JSONResponse(
            status_code=200 if result.replayed else 201,
            content=result.device.model_dump(mode="json"),
        )

    @notification_router.get(
        "/v1/devices",
        openapi_extra={"required_scope": "device.read"},
    )
    async def list_devices(
        authenticated: Annotated[Principal, secured("device.read")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> Page[DeviceView]:
        try:
            return await services.devices.list(authenticated, limit, cursor)
        except ValueError as exc:
            raise MalformedRequestError("device cursor is malformed") from exc

    @notification_router.get(
        "/v1/devices/{device_id}",
        openapi_extra={"required_scope": "device.read"},
    )
    async def get_device(
        device_id: UUID,
        authenticated: Annotated[Principal, secured("device.read")],
    ) -> DeviceView:
        return await services.devices.get(authenticated, device_id)

    @notification_router.post(
        "/v1/devices/{device_id}/revoke",
        openapi_extra={"required_scope": "device.write"},
    )
    async def revoke_device(
        device_id: UUID,
        authenticated: Annotated[Principal, secured("device.write")],
    ) -> DeviceView:
        return await services.devices.revoke(authenticated, device_id)

    @notification_router.delete(
        "/v1/devices/{device_id}",
        status_code=204,
        openapi_extra={"required_scope": "device.write"},
    )
    async def delete_device(
        device_id: UUID,
        authenticated: Annotated[Principal, secured("device.write")],
    ) -> Response:
        await services.devices.delete(authenticated, device_id)
        return Response(status_code=204)

    @notification_router.post(
        "/v1/devices/{device_id}/test-notification",
        openapi_extra={"required_scope": "device.write"},
    )
    async def enqueue_test_notification(
        device_id: UUID,
        authenticated: Annotated[Principal, secured("device.write")],
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
            ),
        ],
    ) -> Response:
        result: TestNotificationResult = await services.devices.enqueue_test_notification(
            authenticated,
            device_id,
            idempotency_key,
        )
        return JSONResponse(
            status_code=200 if result.replayed else 202,
            content=result.model_dump(mode="json", exclude={"replayed"}),
        )

    @notification_router.get(
        "/v1/notifications",
        openapi_extra={"required_scope": "notification.read"},
    )
    async def list_notifications(
        authenticated: Annotated[Principal, secured("notification.read")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> Page[NotificationInboxItem]:
        try:
            return await services.notifications.list(authenticated, limit, cursor)
        except ValueError as exc:
            raise MalformedRequestError("notification cursor is malformed") from exc

    if settings.notification_api_enabled:
        app.include_router(notification_router)

    memory_router = APIRouter()

    @memory_router.get(
        "/v1/memories",
        openapi_extra={"required_scope": "memory.read"},
    )
    async def list_memories(
        response: Response,
        authenticated: Annotated[Principal, secured("memory.read")],
        ceiling: Sensitivity,
        limit: Annotated[int, Query(ge=1)] = 50,
        cursor: str | None = None,
        status: Annotated[list[MemoryStatus] | None, Query()] = None,
        belief_type: Annotated[list[BeliefType] | None, Query()] = None,
        subject: str | None = None,
        session_id: UUID | None = None,
        text: str | None = None,
    ) -> Page[MemoryView]:
        # A belief body is principal-scoped and sensitivity-bearing, so no
        # shared or on-disk cache may keep it; the artifact content route
        # carries the same header for the same reason.
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        # Pagination rule 3 clamps an oversized limit rather than rejecting
        # it; the domain query bounds `limit` at 200, so the clamp happens
        # here, before that value is ever used to construct it.
        try:
            return await services.memory.list(
                authenticated,
                ceiling=ceiling,
                statuses=status,
                belief_types=belief_type,
                subject=subject,
                session_id=session_id,
                text=text,
                limit=min(limit, 200),
                cursor=cursor,
            )
        except ValueError as exc:
            raise MalformedRequestError("memory cursor is malformed") from exc

    @memory_router.get(
        "/v1/memories/{memory_id}",
        openapi_extra={"required_scope": "memory.read"},
    )
    async def get_memory(
        memory_id: UUID,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.read")],
        ceiling: Sensitivity,
    ) -> MemoryView:
        response.headers["Cache-Control"] = PRIVATE_NO_STORE
        return await services.memory.get(authenticated, memory_id, ceiling=ceiling)

    if settings.memory_api_enabled:
        app.include_router(memory_router)

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
