"""Flag-mounted subscription routes. A gesture names senders; it never names a destination."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from agent_core.api.email import RevisionRequest, boundary, private_response
from agent_core.application.services import EmailSubscriptionService
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailOperation
from agent_core.domain.email_subscriptions import SUBSCRIPTION_BATCH_LIMIT

_DIGEST = "^[0-9a-f]{64}$"
SubscriptionId = Annotated[str, Path(pattern=_DIGEST)]
State = Literal[
    "active", "kept", "pending", "unsubscribed", "failed", "still_sending", "reported_spam"
]


class TargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscription_id: str = Field(pattern=_DIGEST)
    evidence_digest: str = Field(pattern=_DIGEST)
    expected_revision: int = Field(ge=1)


class UnsubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: list[TargetRequest] = Field(min_length=1, max_length=SUBSCRIPTION_BATCH_LIMIT)
    archive_existing: bool = Field(default=False, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200)


class SpamRequest(RevisionRequest):
    spam: bool = Field(strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200)


class KeepRequest(RevisionRequest):
    kept: bool = Field(strict=True)


def _same_key(header: str | None, body: str) -> None:
    if header is not None and header != body:
        raise HTTPException(status_code=400, detail="Idempotency keys must match")


def email_subscriptions_router(
    service: EmailSubscriptionService, secured: Callable[[str], object]
) -> APIRouter:
    """Expose the census and the three owner gestures with private responses."""
    router = APIRouter(dependencies=[Depends(private_response)])

    @router.get("/v1/email/subscriptions", openapi_extra={"required_scope": "email.read"})
    async def subscriptions(
        authenticated: Annotated[Principal, secured("email.read")],
        account_id: Annotated[str | None, Query(max_length=32)] = None,
        state: State | None = None,
        cursor: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, object]:
        return await boundary(
            service.browse(
                authenticated, account_id=account_id, state=state, cursor=cursor, limit=limit
            )
        )

    @router.post(
        "/v1/email/subscriptions/unsubscribe", openapi_extra={"required_scope": "email.write"}
    )
    async def unsubscribe(
        body: UnsubscribeRequest,
        authenticated: Annotated[Principal, secured("email.write")],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> EmailOperation:
        """One confirmed gesture is the consent for exactly these senders."""
        _same_key(idempotency_key, body.idempotency_key)
        return await boundary(
            service.unsubscribe(
                authenticated,
                [
                    (target.subscription_id, target.evidence_digest, target.expected_revision)
                    for target in body.targets
                ],
                archive_existing=body.archive_existing,
                idempotency_key=body.idempotency_key,
            )
        )

    @router.post(
        "/v1/email/subscriptions/{subscription_id}/spam",
        openapi_extra={"required_scope": "email.write"},
    )
    async def spam(
        subscription_id: SubscriptionId,
        body: SpamRequest,
        authenticated: Annotated[Principal, secured("email.write")],
        idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
    ) -> EmailOperation:
        """Report spam, or restore exactly the threads a report moved."""
        _same_key(idempotency_key, body.idempotency_key)
        return await boundary(
            service.spam(
                authenticated,
                subscription_id,
                body.expected_revision,
                spam=body.spam,
                idempotency_key=body.idempotency_key,
            )
        )

    @router.post(
        "/v1/email/subscriptions/{subscription_id}/keep",
        openapi_extra={"required_scope": "email.write"},
    )
    async def keep(
        subscription_id: SubscriptionId,
        body: KeepRequest,
        authenticated: Annotated[Principal, secured("email.write")],
    ) -> dict[str, object]:
        """A durable local decision; it changes no mailbox."""
        return await boundary(
            service.keep(authenticated, subscription_id, body.expected_revision, kept=body.kept)
        )

    return router
