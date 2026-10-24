"""Cockpit routes: the board, its stream, assets and the owner's changes (Milestone 35).

docs/plan/cockpit.md, the routes and the stream. The router is mounted only
when `AGENT_COCKPIT_ENABLED=1`. Every answer is private and unstored except an
asset, which is immutable by its digest. The stream completes every check
before its first byte, heartbeats every fifteen seconds and disables proxy
buffering.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import suppress
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse, StreamingResponse

from agent_core.api.boundary import MalformedRequestError
from agent_core.api.sse import encode_sse, heartbeat
from agent_core.application.services import CockpitService
from agent_core.domain.agents import Principal
from agent_core.domain.cockpit import DeviceClass
from agent_core.domain.cockpit_views import (
    CockpitChangeView,
    CockpitLayoutView,
    CockpitLayoutWrite,
    CockpitRevertRequest,
    CockpitSettingsView,
    CockpitSettingsWrite,
    CockpitSnapshotView,
    CockpitWidgetDetailView,
)
from agent_core.domain.views import StreamFrame

HEARTBEAT_SECONDS = 15.0
PRIVATE_NO_STORE = "private, no-store"
IMMUTABLE_PRIVATE = "private, max-age=31536000, immutable"


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE


def _accepted(change: CockpitChangeView) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content=change.model_dump(mode="json"),
        headers={"Cache-Control": PRIVATE_NO_STORE},
    )


def cockpit_router(service: CockpitService, secured: Callable[[str], object]) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/v1/cockpit",
        response_model=CockpitSnapshotView,
        openapi_extra={"required_scope": "cockpit.read"},
        dependencies=[Depends(_private)],
    )
    async def snapshot(
        authenticated: Annotated[Principal, secured("cockpit.read")],
    ) -> CockpitSnapshotView:
        """The board: runtime, live widgets, settings, layouts, feeds and the stream cursor."""
        return await service.snapshot(authenticated)

    @router.get("/v1/cockpit/stream", openapi_extra={"required_scope": "cockpit.read"})
    async def stream(
        authenticated: Annotated[Principal, secured("cockpit.read")],
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """Validate the resume point before sending any event bytes."""
        try:
            after = None if last_event_id is None else int(last_event_id)
        except ValueError as exc:
            raise MalformedRequestError("Last-Event-ID must be an integer") from exc
        if after is not None and after < 0:
            raise MalformedRequestError("Last-Event-ID must not be negative")

        async def frames() -> AsyncIterator[bytes]:
            iterator = cast(
                AsyncGenerator[StreamFrame, None],
                service.stream(authenticated, after).__aiter__(),
            )
            pending: asyncio.Task[StreamFrame] | None = None

            async def next_frame() -> StreamFrame:
                return await iterator.__anext__()

            try:
                while True:
                    if pending is None:
                        pending = asyncio.create_task(next_frame())
                    done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_SECONDS)
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
            headers={"Cache-Control": PRIVATE_NO_STORE, "X-Accel-Buffering": "no"},
        )

    @router.get("/v1/cockpit/assets/{sha256}", openapi_extra={"required_scope": "cockpit.read"})
    async def asset(
        sha256: str,
        authenticated: Annotated[Principal, secured("cockpit.read")],
    ) -> Response:
        """A runtime asset or widget bundle by digest, cached privately and forever."""
        found = await service.asset(authenticated, sha256)
        return Response(
            content=found.content,
            media_type=found.content_type,
            headers={"Cache-Control": IMMUTABLE_PRIVATE, "X-Content-Type-Options": "nosniff"},
        )

    @router.get(
        "/v1/cockpit/widgets/{slug}",
        response_model=CockpitWidgetDetailView,
        openapi_extra={"required_scope": "cockpit.read"},
        dependencies=[Depends(_private)],
    )
    async def widget(
        slug: str,
        authenticated: Annotated[Principal, secured("cockpit.read")],
    ) -> CockpitWidgetDetailView:
        """Revisions with commit links, import failures and the settings schema."""
        return await service.widget(authenticated, slug)

    @router.put(
        "/v1/cockpit/widgets/{slug}/settings",
        response_model=CockpitSettingsView,
        openapi_extra={"required_scope": "cockpit.write"},
        dependencies=[Depends(_private)],
    )
    async def put_settings(
        slug: str,
        request: CockpitSettingsWrite,
        authenticated: Annotated[Principal, secured("cockpit.write")],
    ) -> CockpitSettingsView:
        """Replace a live widget's private settings."""
        return await service.put_settings(authenticated, slug, request.values)

    @router.put(
        "/v1/cockpit/layouts/{device_class}",
        response_model=CockpitLayoutView,
        openapi_extra={"required_scope": "cockpit.write"},
        dependencies=[Depends(_private)],
    )
    async def put_layout(
        device_class: DeviceClass,
        request: CockpitLayoutWrite,
        authenticated: Annotated[Principal, secured("cockpit.write")],
    ) -> CockpitLayoutView:
        """Arrange one device class's board, compare-and-set on its revision."""
        return await service.put_layout(authenticated, device_class, request)

    @router.post(
        "/v1/cockpit/widgets/{slug}/revert",
        openapi_extra={"required_scope": "cockpit.write"},
    )
    async def revert(
        slug: str,
        request: CockpitRevertRequest,
        authenticated: Annotated[Principal, secured("cockpit.write")],
    ) -> JSONResponse:
        """A new revision with a named revision's files, through the whole pipeline."""
        return _accepted(await service.revert(authenticated, slug, request.revision_id))

    @router.post(
        "/v1/cockpit/widgets/{slug}/delete",
        openapi_extra={"required_scope": "cockpit.write"},
    )
    async def delete(
        slug: str,
        authenticated: Annotated[Principal, secured("cockpit.write")],
    ) -> JSONResponse:
        """Queue the widget's removal from the branch."""
        return _accepted(await service.delete(authenticated, slug))

    @router.post(
        "/v1/cockpit/widgets/{slug}/restore",
        openapi_extra={"required_scope": "cockpit.write"},
    )
    async def restore(
        slug: str,
        authenticated: Annotated[Principal, secured("cockpit.write")],
    ) -> JSONResponse:
        """Commit a deleted widget's last live package again."""
        return _accepted(await service.restore(authenticated, slug))

    return router
