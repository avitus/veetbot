"""Flag-mounted chat attachment upload (ADR-0118). The body is the file itself."""

from collections.abc import Callable
from typing import Annotated
from urllib.parse import unquote
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from agent_core.application.services import ArtifactService
from agent_core.domain.agents import Principal
from agent_core.domain.attachments import upload_key, upload_media_type, upload_name
from agent_core.domain.errors import AttachmentValidationError

_OCTET_STREAM = "application/octet-stream"


def _filename(header: str | None) -> str:
    if header is None:
        raise AttachmentValidationError("An upload requires an X-Filename header.")
    try:
        decoded = unquote(header, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AttachmentValidationError("X-Filename must be percent-encoded UTF-8.") from exc
    return upload_name(decoded)


def attachments_router(service: ArtifactService, secured: Callable[[str], object]) -> APIRouter:
    """Expose the one upload route; headers are checked before the body is read."""
    router = APIRouter()

    @router.post(
        "/v1/sessions/{session_id}/artifacts",
        status_code=201,
        openapi_extra={"required_scope": "artifact.write"},
    )
    async def upload_attachment(
        session_id: UUID,
        request: Request,
        authenticated: Annotated[Principal, secured("artifact.write")],
        x_filename: Annotated[str | None, Header(alias="X-Filename")] = None,
        content_type: Annotated[str | None, Header(alias="Content-Type")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        """Store one file for the session; sending a message claims it."""
        filename = _filename(x_filename)
        media_type = upload_media_type(content_type or _OCTET_STREAM)
        key = upload_key(idempotency_key)
        content = await request.body()
        view, replayed = await service.upload(
            authenticated,
            session_id,
            content=content,
            filename=filename,
            declared_media_type=media_type,
            idempotency_key=key,
        )
        return JSONResponse(
            status_code=200 if replayed else 201,
            content=view.model_dump(mode="json"),
            headers={
                "Cache-Control": "private, no-store",
                "Location": f"/v1/artifacts/{view.id}",
            },
        )

    return router
