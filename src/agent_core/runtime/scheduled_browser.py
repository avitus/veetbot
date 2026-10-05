"""Validate scheduled website bindings on the run worker, before model work."""

from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserProfileStatus
from agent_core.domain.context import ContextPlan
from agent_core.domain.errors import NotFoundError
from agent_core.domain.sessions import (
    SESSION_BROWSER_PROFILE_METADATA_KEY,
    SESSION_SCHEDULE_ID_METADATA_KEY,
    Session,
)
from agent_core.ports.persistence import RepositoryUnitOfWork


class ScheduledBrowserUnavailableError(Exception):
    """A stable, content-free scheduled browser startup failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def is_browser_schedule(session: Session) -> bool:
    return (
        SESSION_SCHEDULE_ID_METADATA_KEY in session.metadata
        and SESSION_BROWSER_PROFILE_METADATA_KEY in session.metadata
    )


async def validate_scheduled_browser(
    uow: RepositoryUnitOfWork, session: Session, principal: Principal
) -> None:
    if not is_browser_schedule(session):
        return
    if "browser.profile.read" not in principal.scopes:
        raise ScheduledBrowserUnavailableError("policy.scope.missing")
    try:
        profile_id = UUID(str(session.metadata[SESSION_BROWSER_PROFILE_METADATA_KEY]))
        profile = await uow.browser_profiles.get(profile_id, principal)
    except (NotFoundError, ValueError) as exc:
        raise ScheduledBrowserUnavailableError("tool.browser.profile_unavailable") from exc
    if profile.status is BrowserProfileStatus.AUTHENTICATION_REQUIRED:
        raise ScheduledBrowserUnavailableError("tool.browser.authentication_required")
    if profile.status is BrowserProfileStatus.NEEDS_USER:
        raise ScheduledBrowserUnavailableError("tool.browser.needs_user")
    if profile.status is not BrowserProfileStatus.READY:
        raise ScheduledBrowserUnavailableError("tool.browser.profile_unavailable")


def require_scheduled_browser_tools(session: Session, plan: ContextPlan) -> None:
    if is_browser_schedule(session) and not {"browser.navigate", "browser.observe"} <= set(
        plan.tool_names
    ):
        raise ScheduledBrowserUnavailableError("tool.browser.provider_unavailable")
