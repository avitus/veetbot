"""Flag-mounted browser task-grant routes (ADR-0129 section 8.3).

List, read and revoke only: a task grant is created from an approval card,
never through its own route. Mounted only when BROWSER_TASK_GRANTS_ENABLED
is set, so the default route census is unchanged.
"""

from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query

from agent_core.api.boundary import MalformedRequestError
from agent_core.application.services import BrowserTaskGrantService
from agent_core.domain.agents import Principal
from agent_core.domain.browser_task_grants import BrowserTaskGrantView
from agent_core.domain.views import Page


def browser_task_grants_router(
    service: BrowserTaskGrantService, secured: Callable[[str], object]
) -> APIRouter:
    """Expose the three task-grant routes under the existing grant scopes."""
    router = APIRouter()

    @router.get(
        "/v1/browser-task-grants",
        openapi_extra={"required_scope": "browser.grant.read"},
    )
    async def list_browser_task_grants(
        authenticated: Annotated[Principal, secured("browser.grant.read")],
        session_id: UUID | None = None,
        status: Literal["active", "all"] = "active",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> Page[BrowserTaskGrantView]:
        """List the principal's task grants, newest first."""
        try:
            return await service.list(
                authenticated, session_id=session_id, status=status, limit=limit, cursor=cursor
            )
        except ValueError as exc:
            raise MalformedRequestError("browser task grant cursor is malformed") from exc

    @router.get(
        "/v1/browser-task-grants/{grant_id}",
        openapi_extra={"required_scope": "browser.grant.read"},
    )
    async def get_browser_task_grant(
        grant_id: UUID,
        authenticated: Annotated[Principal, secured("browser.grant.read")],
    ) -> BrowserTaskGrantView:
        """Read one owned task grant and its derived status."""
        return await service.get(authenticated, grant_id)

    @router.post(
        "/v1/browser-task-grants/{grant_id}/revoke",
        openapi_extra={"required_scope": "browser.grant.write"},
    )
    async def revoke_browser_task_grant(
        grant_id: UUID,
        authenticated: Annotated[Principal, secured("browser.grant.write")],
    ) -> BrowserTaskGrantView:
        """End an active task grant now; an ended grant returns unchanged."""
        return await service.revoke(authenticated, grant_id)

    return router
