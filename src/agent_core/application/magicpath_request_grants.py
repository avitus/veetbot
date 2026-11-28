"""One owner approval for the MagicPath operations of a design request."""

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from agent_core.application.browser_request_grants import approval_allowed_for_request
from agent_core.application.script_request_grants import owner_request, script_card_is_covered
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.magicpath_request_grants import (
    MAGICPATH_ENDPOINT,
    MAGICPATH_GRANT_CREATED,
    MAGICPATH_TOOLS,
    PROJECT_LOOKUPS,
    MagicPathRequestGrantOffer,
    MagicPathToolName,
)
from agent_core.domain.mcp import MCPAuthScheme
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
)
from agent_core.domain.runs import Run
from agent_core.domain.script_request_grants import SCRIPT_REQUEST_GRANT_DURATION
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


async def magicpath_login(uow: RepositoryUnitOfWork, principal: Principal) -> datetime | None:
    config = await uow.mcp_servers.get(principal.tenant_id, "magicpath")
    if not (
        config
        and config.owner_managed
        and config.enabled
        and config.endpoint == MAGICPATH_ENDPOINT
        and config.auth_scheme is MCPAuthScheme.OAUTH2_AUTHORIZATION
    ):
        return None
    auth = await uow.mcp_servers.get_authorization(principal.tenant_id, "magicpath")
    return (
        auth.signed_in_at
        if auth and auth.signed_in and auth.signed_in_by == principal.principal_id
        else None
    )


def _canvas_card(name: str, decision: PolicyDecision) -> bool:
    return (
        name in MAGICPATH_TOOLS
        and decision.decision is PolicyDecisionType.REQUIRE_APPROVAL
        and decision.reason_code == "policy.matrix.external_write"
    )


def _project(arguments: dict[str, Any]) -> str | None:
    project = arguments.get("projectId")
    return (
        project
        if isinstance(project, str)
        and project.isascii()
        and project.isdecimal()
        and len(project) <= 128
        else None
    )


class MagicPathRequestGrants:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        now: Callable[[], datetime],
        pinned_catalog: Callable[[UUID, str], str | None],
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now
        self._pinned_catalog = pinned_catalog

    async def offer(
        self,
        name: str,
        arguments: dict[str, Any],
        decision: PolicyDecision,
        run: Run,
        principal: Principal,
        turn: AuthorizationTurn | None,
    ) -> MagicPathRequestGrantOffer | None:
        if not _canvas_card(name, decision):
            return None
        project = _project(arguments)
        if name not in PROJECT_LOOKUPS and project is None:
            return None
        catalog = self._pinned_catalog(run.session_id, name)
        if catalog is None:
            return None
        async with self._uow_factory() as uow:
            if not await owner_request(uow, run, principal, turn):
                return None
            login = await magicpath_login(uow, principal)
        if login is None:
            return None
        target = f"project {project}" if project else "the first project Veetbot opens"
        return MagicPathRequestGrantOffer(
            tool_name=cast(MagicPathToolName, name),
            catalog_hash=catalog,
            signed_in_at=login,
            project_id=project,
            summary=(
                "Allow Veetbot to list MagicPath projects, read the canvas and create shapes in "
                f"{target}, and run sandbox scripts for this request. Other tool calls keep "
                "their usual approvals."
            ),
        )

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
        refused = StandingAuthorization(allowed=False, reason_code="magicpath.request_grant.none")
        script = script_card_is_covered(action.name, decision)
        if not script and not _canvas_card(action.name, decision):
            return refused
        async with self._uow_factory() as uow:
            card = await approval_allowed_for_request(uow, run, principal, MAGICPATH_GRANT_CREATED)
            if card is None or card.resolved_at is None:
                return refused
            offer = card.script_request_grant_offer
            if (
                not isinstance(offer, MagicPathRequestGrantOffer)
                or card.tool_name != offer.tool_name
                or card.agent_version != agent_version
                or card.policy_version != decision.policy_version
                or self._now() >= card.resolved_at + SCRIPT_REQUEST_GRANT_DURATION
                or not await owner_request(uow, run, principal, turn)
                or await magicpath_login(uow, principal) != offer.signed_in_at
            ):
                return refused
            pinned_name = offer.tool_name if script else action.name
            if self._pinned_catalog(run.session_id, pinned_name) != offer.catalog_hash:
                return refused
            if not script and action.name not in PROJECT_LOOKUPS:
                project = _project(action.arguments)
                if project is None or (
                    offer.project_id is not None and project != offer.project_id
                ):
                    return refused
                # Discovery cards have no project yet. Atomic append returns the first
                # binding even if two calls race or the worker retries after a restart.
                if offer.project_id is None:
                    bound = await uow.events.append(
                        NewEvent(
                            session_id=run.session_id,
                            run_id=run.id,
                            actor_type="application",
                            event_type="magicpath.request_grant.project_bound",
                            derivation_key=f"magicpath-project:{card.id}",
                            payload={"project_id": project},
                        )
                    )
                    if bound.payload.get("project_id") != project:
                        return refused
        return StandingAuthorization(
            allowed=True,
            reason_code="magicpath.request_grant.authorized",
            authorization_kind="magicpath_request_grant",
            authorization_ref=str(card.id),
        )
