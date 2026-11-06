"""ADR-0302: the public boundary of one approval per request.

The resolve body carries ``request_grant`` exactly when the decision is
``approve_for_request``; the approval view carries the offer additively.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from agent_core.api import create_app
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser_request_grants import (
    BrowserRequestGrantOffer,
    RequestGrantEcho,
    request_grant_summary,
)
from agent_core.domain.browser_task_grants import TaskGrantEcho
from agent_core.domain.views import ApprovalView
from tests.contract.test_browser_task_grant_api_contract import (
    APPROVAL_ID,
    RESOLVE,
    _ready,
    approval_view,
    owner,
    post,
    services,
    settings,
    specified_conflict_details,
)

PROFILE = UUID("00000000-0000-0000-0000-0000000001b2")
OFFER = BrowserRequestGrantOffer(
    profile_id=PROFILE,
    profile_generation=5,
    origins=("https://secure.chase.com",),
    summary=request_grant_summary(("https://secure.chase.com",)),
)


class RecordingApprovals:
    def __init__(self) -> None:
        self.resolved: list[tuple[ApprovalResolutionType, Any, Any]] = []

    async def resolve(
        self,
        principal: Principal,
        approval_id: UUID,
        decision: ApprovalResolutionType,
        reason: str | None,
        *,
        task_grant: TaskGrantEcho | None = None,
        request_grant: RequestGrantEcho | None = None,
    ) -> ApprovalView:
        del principal, reason
        assert approval_id == APPROVAL_ID
        self.resolved.append((decision, task_grant, request_grant))
        return approval_view(decision).model_copy(update={"request_grant_offer": OFFER})


def app(approvals: RecordingApprovals) -> Any:
    return create_app(
        services(approvals),
        settings(),
        owner("approval.resolve", "browser.grant.write"),
        lambda: str(APPROVAL_ID),
        _ready,
    )


async def test_approve_for_request_carries_the_offered_profile() -> None:
    approvals = RecordingApprovals()

    response = await post(
        app(approvals),
        RESOLVE,
        {"decision": "approve_for_request", "request_grant": {"profile_id": str(PROFILE)}},
    )

    assert response.status_code == 200, response.text
    assert approvals.resolved == [
        (ApprovalResolutionType.APPROVE_FOR_REQUEST, None, RequestGrantEcho(profile_id=PROFILE))
    ]
    body = response.json()
    assert body["decision"] == "approve_for_request"
    assert body["request_grant_offer"]["profile_id"] == str(PROFILE)
    assert body["request_grant_offer"]["origins"] == ["https://secure.chase.com"]
    assert body["request_grant_offer"]["duration_seconds"] == 7200


async def test_request_grant_is_required_exactly_with_approve_for_request() -> None:
    approvals = RecordingApprovals()
    echo = {"profile_id": str(PROFILE)}

    missing = await post(app(approvals), RESOLVE, {"decision": "approve_for_request"})
    stray = await post(app(approvals), RESOLVE, {"decision": "approve_once", "request_grant": echo})

    assert (missing.status_code, stray.status_code) == (400, 400)
    assert approvals.resolved == []


async def test_an_ordinary_decision_still_reaches_the_service_unchanged() -> None:
    approvals = RecordingApprovals()

    response = await post(app(approvals), RESOLVE, {"decision": "approve_once"})

    assert response.status_code == 200, response.text
    assert approvals.resolved == [(ApprovalResolutionType.APPROVE_ONCE, None, None)]


def test_the_http_spec_lists_the_request_grant_conflicts() -> None:
    carried = specified_conflict_details()

    assert carried["request_grant_unavailable"] == {"approval_id"}
    assert carried["request_grant_offer_mismatch"] == {"approval_id"}
