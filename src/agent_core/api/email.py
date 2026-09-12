"""Flag-mounted Email API. Exact authorization and private cache semantics."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from agent_core.application.services import EmailService
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailDraft, EmailDraftEdit, EmailLearningState, EmailOperation


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: UUID
    target: Literal["thread", "person", "topic"]
    judgment: Literal["important", "less_important", "needs_reply", "no_reply_needed"]
    explanation: str | None = Field(default=None, max_length=2000)
    target_value: str | None = Field(default=None, max_length=320)
    expected_revision: int | None = Field(default=None, ge=1)


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str | None = Field(default=None, max_length=4000)


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)


class DismissRequest(RevisionRequest):
    dismissed: bool = Field(default=True, strict=True)


class LearningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


class ResetLearningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["preferences", "style", "all"]


async def boundary[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="email request is malformed") from exc


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def email_router(service: EmailService, secured: Callable[[str], object]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(private_response)])

    @router.get("/v1/email/accounts", openapi_extra={"required_scope": "email.read"})
    async def accounts(
        response: Response,
        authenticated: Annotated[Principal, secured("email.read")],
    ) -> dict[str, object]:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.accounts(authenticated)

    @router.get("/v1/email/threads", openapi_extra={"required_scope": "email.read"})
    async def threads(
        authenticated: Annotated[Principal, secured("email.read")],
        view: Literal["priority", "other", "all"] = "priority",
        account_id: str | None = None,
        text: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 5,
    ) -> dict[str, object]:
        return await boundary(
            service.threads(
                authenticated,
                view=view,
                account_id=account_id,
                text=text,
                cursor=cursor,
                limit=limit,
            )
        )

    @router.get("/v1/email/threads/{thread_id}", openapi_extra={"required_scope": "email.read"})
    async def thread(
        thread_id: UUID,
        authenticated: Annotated[Principal, secured("email.read")],
    ) -> dict[str, object]:
        return await service.thread(authenticated, thread_id)

    @router.post("/v1/email/feedback", openapi_extra={"required_scope": "email.write"})
    async def feedback(
        body: FeedbackRequest,
        authenticated: Annotated[Principal, secured("email.write")],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> dict[str, object]:
        return await boundary(
            service.feedback(
                authenticated,
                thread_id=body.thread_id,
                target=body.target,
                judgment=body.judgment,
                explanation=body.explanation,
                target_value=body.target_value,
                expected_revision=body.expected_revision,
                idempotency_key=idempotency_key,
            )
        )

    @router.delete(
        "/v1/email/feedback/{feedback_id}", openapi_extra={"required_scope": "email.write"}
    )
    async def undo_feedback(
        feedback_id: UUID,
        authenticated: Annotated[Principal, secured("email.write")],
    ) -> dict[str, object]:
        return await service.undo_feedback(authenticated, feedback_id)

    @router.get("/v1/email/drafts/{draft_id}", openapi_extra={"required_scope": "email.read"})
    async def draft(
        draft_id: UUID,
        authenticated: Annotated[Principal, secured("email.read")],
    ) -> EmailDraft:
        return await service.draft(authenticated, draft_id)

    @router.put("/v1/email/drafts/{draft_id}", openapi_extra={"required_scope": "email.write"})
    async def edit_draft(
        draft_id: UUID,
        body: EmailDraftEdit,
        authenticated: Annotated[Principal, secured("email.write")],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> EmailDraft:
        return await boundary(
            service.edit_draft(authenticated, draft_id, body, idempotency_key=idempotency_key)
        )

    @router.get(
        "/v1/email/drafts/{draft_id}/revisions", openapi_extra={"required_scope": "email.read"}
    )
    async def draft_revisions(
        draft_id: UUID,
        authenticated: Annotated[Principal, secured("email.read")],
    ) -> dict[str, object]:
        return await service.draft_revisions(authenticated, draft_id)

    @router.post("/v1/email/refresh", openapi_extra={"required_scope": "email.write"})
    async def refresh(
        authenticated: Annotated[Principal, secured("email.write")],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> EmailOperation:
        return await service.submit_task(
            authenticated, kind="refresh", idempotency_key=idempotency_key
        )

    @router.get(
        "/v1/email/operations/{operation_id}", openapi_extra={"required_scope": "email.read"}
    )
    async def operation(
        operation_id: UUID,
        authenticated: Annotated[Principal, secured("email.read")],
    ) -> EmailOperation:
        return await service.operation(authenticated, operation_id)

    @router.get("/v1/email/learning", openapi_extra={"required_scope": "email.read"})
    async def learning(
        authenticated: Annotated[Principal, secured("email.read")],
    ) -> EmailLearningState:
        return await service.learning(authenticated)

    @router.put("/v1/email/learning", openapi_extra={"required_scope": "email.write"})
    async def pause_learning(
        body: LearningRequest, authenticated: Annotated[Principal, secured("email.write")]
    ) -> EmailLearningState:
        return await service.pause_learning(authenticated, body.paused)

    @router.post("/v1/email/learning/reset", openapi_extra={"required_scope": "email.write"})
    async def reset_learning(
        body: ResetLearningRequest, authenticated: Annotated[Principal, secured("email.write")]
    ) -> EmailLearningState:
        return await service.reset_learning(authenticated, body.scope)

    @router.post(
        "/v1/email/threads/{thread_id}/drafts", openapi_extra={"required_scope": "email.write"}
    )
    async def create_draft(
        thread_id: UUID,
        body: DraftRequest,
        authenticated: Annotated[Principal, secured("email.write")],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> dict[str, object]:
        # This command returns private thread content. Authorize that response
        # before admitting a durable operation, including on idempotent retries.
        await service.thread(authenticated, thread_id)
        operation = await service.submit_task(
            authenticated,
            kind="draft",
            thread_id=thread_id,
            instruction=body.instruction,
            idempotency_key=idempotency_key,
        )
        thread = await service.thread(authenticated, thread_id)
        return {
            "draft": thread["draft"],
            "run_id": str(operation.run_id),
            "status": operation.status,
        }

    @router.post(
        "/v1/email/drafts/{draft_id}/send-proposal", openapi_extra={"required_scope": "email.write"}
    )
    async def send_proposal(
        draft_id: UUID,
        body: RevisionRequest,
        authenticated: Annotated[Principal, secured("email.write")],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> dict[str, object]:
        await service.draft(authenticated, draft_id)
        operation = await service.submit_task(
            authenticated,
            kind="send",
            draft_id=draft_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )
        draft = await service.draft(authenticated, draft_id)
        return {
            "draft": draft.model_dump(mode="json"),
            "run_id": str(operation.run_id),
            "status": operation.status,
            "approval_id": None if draft.approval_id is None else str(draft.approval_id),
        }

    @router.post(
        "/v1/email/threads/{thread_id}/discussion", openapi_extra={"required_scope": "email.write"}
    )
    async def discussion(
        thread_id: UUID, authenticated: Annotated[Principal, secured("email.write")]
    ) -> dict[str, object]:
        return await service.discussion(authenticated, thread_id)

    @router.post(
        "/v1/email/threads/{thread_id}/dismiss", openapi_extra={"required_scope": "email.write"}
    )
    async def dismiss(
        thread_id: UUID,
        body: DismissRequest,
        authenticated: Annotated[Principal, secured("email.write")],
    ) -> dict[str, object]:
        return await service.dismiss(
            authenticated, thread_id, body.expected_revision, dismissed=body.dismissed
        )

    @router.delete("/v1/email/drafts/{draft_id}", openapi_extra={"required_scope": "email.write"})
    async def discard_draft(
        draft_id: UUID,
        authenticated: Annotated[Principal, secured("email.write")],
        expected_revision: Annotated[int, Query(ge=1)],
    ) -> EmailDraft:
        return await service.discard_draft(authenticated, draft_id, expected_revision)

    @router.post(
        "/v1/email/threads/{thread_id}/exclude", openapi_extra={"required_scope": "email.write"}
    )
    async def exclude_source(
        thread_id: UUID,
        body: RevisionRequest,
        authenticated: Annotated[Principal, secured("email.write")],
    ) -> dict[str, object]:
        return await service.exclude_source(authenticated, thread_id, body.expected_revision)

    @router.post(
        "/v1/email/drafts/{draft_id}/style-example",
        openapi_extra={"required_scope": "email.write"},
    )
    async def endorse_style(
        draft_id: UUID,
        body: RevisionRequest,
        authenticated: Annotated[Principal, secured("email.write")],
    ) -> EmailLearningState:
        return await boundary(
            service.endorse_style(authenticated, draft_id, body.expected_revision)
        )

    return router
