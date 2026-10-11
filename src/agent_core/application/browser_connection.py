"""Authenticated website selection at conversation admission boundaries."""

from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserProfileStatus
from agent_core.domain.errors import AuthorizationError, ConflictError, InvalidStateTransition
from agent_core.domain.events import NewEvent
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.ports.persistence import RepositoryUnitOfWork


async def bind_chat_website(
    uow: RepositoryUnitOfWork, principal: Principal, session: Session, profile_id: UUID
) -> Session:
    """Caller holds the session admission lock; no live run can change accounts."""
    if not {"session.write", "browser.profile.read"} <= principal.scopes:
        raise AuthorizationError(
            "website selection requires session.write and browser.profile.read"
        )
    if session.status is SessionStatus.CLOSED:
        raise InvalidStateTransition("closed conversations cannot connect websites")
    profile = await uow.browser_profiles.get(profile_id, principal)
    if profile.status not in {
        BrowserProfileStatus.READY,
        BrowserProfileStatus.AUTHENTICATION_REQUIRED,
        BrowserProfileStatus.NEEDS_USER,
    }:
        raise InvalidStateTransition("website is unavailable", reason="browser_profile_not_ready")
    if session.metadata.get("browser_profile_id") == str(profile_id):
        return session
    if await uow.runs.active_for_session(session.id, principal) is not None:
        raise ConflictError(
            "Finish or cancel the current run before changing its website.",
            reason="active_run_exists",
        )
    bound = await uow.sessions.bind_browser_profile(session.id, principal, profile_id)
    await uow.events.append(
        NewEvent(
            session_id=session.id,
            run_id=None,
            event_type="session.browser.connected",
            actor_type="principal",
            actor_id=principal.principal_id,
            payload={"profile_id": str(profile_id)},
        )
    )
    return bound


async def prepare_follow_request(
    uow: RepositoryUnitOfWork, principal: Principal, session: Session, text: str
) -> tuple[Session, str | None]:
    from agent_core.domain.browser_follow import follow_request
    from agent_core.domain.events import conversation_items
    from agent_core.domain.messages import AssistantMessage, TextPart

    agent = await uow.agents.get_version(session.agent_id, session.agent_version)
    if "browser.act" not in agent.enabled_tools or "browser.profile.read" not in principal.scopes:
        return session, None
    previous = await uow.events.latest_before(
        session.id, (1 << 63) - 1, "assistant.message.completed", principal
    )
    answer = ""
    if previous is not None:
        answer = "\n".join(
            part.text
            for item in conversation_items(previous)
            if isinstance(item, AssistantMessage)
            for part in item.content
            if isinstance(part, TextPart)
        )
    handle = follow_request(text, answer)
    if handle is None:
        return session, None
    selected = session.metadata.get("browser_profile_id")
    if selected is not None:
        profile = await uow.browser_profiles.get(UUID(str(selected)), principal)
        if "https://x.com" in profile.allowed_origins:
            return session, handle
        raise ConflictError(
            "Connect your X account to follow this recommendation.",
            reason="browser_profile_mismatch",
        )
    profiles = await uow.browser_profiles.list(principal, limit=65)
    # A bounded list that may be truncated cannot prove there is one account.
    if len(profiles) == 65:
        return session, handle
    candidates = [
        p
        for p in profiles
        if "https://x.com" in p.allowed_origins
        and p.status
        in {
            BrowserProfileStatus.READY,
            BrowserProfileStatus.AUTHENTICATION_REQUIRED,
            BrowserProfileStatus.NEEDS_USER,
        }
    ]
    if len(candidates) == 1:
        session = await bind_chat_website(uow, principal, session, candidates[0].id)
    return session, handle
