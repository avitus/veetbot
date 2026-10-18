"""ADR-0213: the owner's own request for a call is its authorization."""

from collections.abc import Callable
from datetime import datetime

from agent_core.domain.agents import Principal
from agent_core.domain.argument_views import approval_argument_view
from agent_core.domain.errors import NotFoundError
from agent_core.domain.policies import (
    ActionKind,
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    SideEffectClass,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind
from agent_core.domain.sessions import SESSION_SCHEDULE_ID_METADATA_KEY
from agent_core.ports.persistence import UnitOfWorkFactory

OWNER_CALL_AUTHORIZATION_KIND = "owner_call_request"
START_CALL_TOOL_NAME = "mcp.bland_call.start_call"
CALL_SCOPE = "mcp.bland_call.use"
SCHEDULE_CREATE_TOOL_NAME = "schedule.create"
# The owner's message, and the owner's own memory or People lookup of a number.
# A turn that read email, the web, a document, or a call transcript is not here:
# its call keeps the confirmation card, so content cannot place a call.
_OWNER_ORIGINS = frozenset(
    {
        TrustLevel.PLATFORM,
        TrustLevel.TRUSTED_CONFIGURATION,
        TrustLevel.USER,
        TrustLevel.MEMORY,
    }
)


class OwnerCallAuthorizer:
    """Composed only with calling enabled; a refusal uses ordinary approval."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, now: Callable[[], datetime]) -> None:
        self._uow_factory = uow_factory
        self._now = now

    async def authorize(
        self,
        *,
        action: ProposedAction,
        decision: PolicyDecision,
        principal: Principal,
        run: Run,
        agent_version: str,
        action_deadline: datetime,
        turn: AuthorizationTurn | None = None,
    ) -> StandingAuthorization:
        """Authorize a call, or a call schedule, that the owner's own turn asked for."""
        refused = StandingAuthorization(allowed=False, reason_code="call.owner_request.not_covered")
        if not (
            _is_call(action)
            and action.kind is ActionKind.TOOL_CALL
            and CALL_SCOPE in principal.scopes
            and decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
            and run.kind is RunKind.INTERACTIVE
            and run.parent_run_id is None
            and run.tenant_id == principal.tenant_id
            and run.agent_version == agent_version
            and turn is not None
            and turn.newest_user_trust is TrustLevel.USER
            and action.origin_trust in _OWNER_ORIGINS
            and self._now() < action_deadline
        ):
            return refused
        try:
            async with self._uow_factory() as uow:
                session = await uow.sessions.get(run.session_id, principal)
                seed = await uow.events.latest_before(
                    session.id, run.seed_event_sequence + 1, "user.message.created", principal
                )
        except NotFoundError:
            return refused
        if not (
            session.principal_id == principal.principal_id
            and seed is not None
            and seed.run_id == run.id
            and seed.sequence == run.seed_event_sequence
            and seed.actor_id == principal.principal_id
            and (
                seed.actor_type == "principal"
                or (
                    seed.actor_type == "scheduler"
                    and SESSION_SCHEDULE_ID_METADATA_KEY in session.metadata
                )
            )
        ):
            return refused
        return StandingAuthorization(
            allowed=True,
            reason_code="call.owner_request.authorized",
            authorization_kind=OWNER_CALL_AUTHORIZATION_KIND,
            authorization_ref=f"{run.id}:{seed.sequence}",
            authorization_view=approval_argument_view(action.arguments),
        )


def _is_call(action: ProposedAction) -> bool:
    if action.name == START_CALL_TOOL_NAME:
        return (
            action.target.kind == "mcp"
            and action.target.server_id == "bland_call"
            and action.side_effect is SideEffectClass.EXTERNAL_MESSAGE
        )
    # A call schedule carries calling authority and nothing else.
    return (
        action.name == SCHEDULE_CREATE_TOOL_NAME
        and action.side_effect is SideEffectClass.EXTERNAL_WRITE
        and action.arguments.get("place_call") is True
        and not action.arguments.get("use_website")
    )
