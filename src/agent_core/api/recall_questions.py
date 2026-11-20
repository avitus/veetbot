"""Owner recall question routes and a content-free page shell (ADR-0339)."""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_core.application.services import RecallQuestionService
from agent_core.domain.agents import Principal
from agent_core.domain.recall_questions import (
    QUESTION_KEY_PATTERN,
    QuestionSplit,
    QuestionStatus,
    RecallQuestionContent,
    RecallQuestionHistory,
    RecallQuestionList,
    RecallQuestionView,
)

_ASSETS = Path(__file__).with_name("recall_questions_web")
# The shell holds no owner data and runs only its own script: no inline code,
# no form posts, no framing, and requests to this origin alone.
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
_PRIVATE = "private, no-store"


class CreateRecallQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=80, pattern=QUESTION_KEY_PATTERN)
    # Chosen once: no later request can move a question to the other half.
    split: QuestionSplit
    content: RecallQuestionContent


class EditRecallQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    content: RecallQuestionContent


class RecallQuestionStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    status: QuestionStatus


def recall_questions_router(
    service: RecallQuestionService, secured: Callable[[str], object]
) -> APIRouter:
    router = APIRouter()

    @router.get("/recall-questions", include_in_schema=False)
    async def shell() -> FileResponse:
        return FileResponse(_ASSETS / "index.html", headers=_HEADERS)

    @router.get("/recall-questions/app.js", include_in_schema=False)
    async def script() -> FileResponse:
        return FileResponse(_ASSETS / "app.js", media_type="text/javascript", headers=_HEADERS)

    @router.get("/recall-questions/style.css", include_in_schema=False)
    async def style() -> FileResponse:
        return FileResponse(_ASSETS / "style.css", media_type="text/css", headers=_HEADERS)

    @router.get("/v1/recall-questions", openapi_extra={"required_scope": "memory.read"})
    async def list_questions(
        response: Response,
        authenticated: Annotated[Principal, secured("memory.read")],
        split: QuestionSplit | None = None,
        status: QuestionStatus | None = None,
    ) -> RecallQuestionList:
        response.headers["Cache-Control"] = _PRIVATE
        return await service.list_questions(authenticated, split=split, status=status)

    @router.post(
        "/v1/recall-questions",
        status_code=201,
        openapi_extra={"required_scope": "memory.write"},
    )
    async def create_question(
        body: CreateRecallQuestionRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.write")],
    ) -> RecallQuestionView:
        response.headers["Cache-Control"] = _PRIVATE
        return await service.create(
            authenticated, key=body.key, split=body.split, content=body.content
        )

    @router.put(
        "/v1/recall-questions/{question_id}", openapi_extra={"required_scope": "memory.write"}
    )
    async def edit_question(
        question_id: UUID,
        body: EditRecallQuestionRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.write")],
    ) -> RecallQuestionView:
        response.headers["Cache-Control"] = _PRIVATE
        return await service.edit(
            authenticated,
            question_id,
            expected_revision=body.expected_revision,
            content=body.content,
        )

    @router.post(
        "/v1/recall-questions/{question_id}/status",
        openapi_extra={"required_scope": "memory.write"},
    )
    async def set_question_status(
        question_id: UUID,
        body: RecallQuestionStatusRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.write")],
    ) -> RecallQuestionView:
        response.headers["Cache-Control"] = _PRIVATE
        return await service.set_status(
            authenticated,
            question_id,
            expected_revision=body.expected_revision,
            status=body.status,
        )

    @router.get(
        "/v1/recall-questions/{question_id}/history",
        openapi_extra={"required_scope": "memory.read"},
    )
    async def question_history(
        question_id: UUID,
        response: Response,
        authenticated: Annotated[Principal, secured("memory.read")],
    ) -> RecallQuestionHistory:
        response.headers["Cache-Control"] = _PRIVATE
        return await service.history(authenticated, question_id)

    return router
