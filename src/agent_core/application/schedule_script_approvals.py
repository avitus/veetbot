"""A schedule's script approval, given once at setup (ADR-0342 decision 13).

A scheduled run has nobody to answer a card. The owner approves a schedule's
scripts when the schedule is set up, and the approval is stored on the
schedule's revision. A run of that revision then starts a script without a
card, including one written after the run read outside content. Nothing else
is covered: every call the script makes is still decided by policy.
"""

from __future__ import annotations

from datetime import datetime

from agent_core.application.script_request_grants import script_card_is_covered
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.execution import SANDBOX_EXECUTE_SCOPE, SANDBOX_RUN_CODE_TOOL_NAME
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    ProposedAction,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run
from agent_core.domain.schedules import JobTask, ScheduleRecord, ScheduleRevision
from agent_core.ports.persistence import UnitOfWorkFactory

SCHEDULE_SCRIPT_APPROVAL_AUTHORIZATION_KIND = "schedule_script_approval"
SCHEDULE_SCRIPT_APPROVAL_AUTHORIZED_REASON = "script.schedule_approval.authorized"
NO_SCHEDULE_SCRIPT_APPROVAL = "script.schedule_approval.not_covered"


class ScheduleScriptAuthorizer:
    """Authorize a script in a run of a schedule the owner approved for scripts."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory) -> None:
        """Keep the store that links a run to its schedule revision."""

        self._uow_factory = uow_factory

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
        """Allow the script only when this run's own schedule revision holds the approval."""

        del action_deadline
        denied = StandingAuthorization(allowed=False, reason_code=NO_SCHEDULE_SCRIPT_APPROVAL)
        if not script_card_is_covered(action.name, decision):
            return denied
        # The schedule's own run, in a turn the schedule's instruction opened.
        if (
            run.parent_run_id is not None
            or run.tenant_id != principal.tenant_id
            or turn is None
            or turn.newest_user_trust is not TrustLevel.USER
        ):
            return denied
        async with self._uow_factory() as uow:
            # The stored link, never the session's metadata, says whose run this is.
            link = await uow.schedule_occurrences.get_by_run(run.id)
            if link is None or (link.tenant_id, link.principal_id) != (
                principal.tenant_id,
                principal.principal_id,
            ):
                return denied
            occurrence = link.occurrence
            try:
                revision = await uow.schedules.get_revision(
                    occurrence.schedule_id, occurrence.schedule_revision, principal
                )
            except NotFoundError:
                return denied
        if not revision.scripts_approved or revision.agent_version != agent_version:
            return denied
        return StandingAuthorization(
            allowed=True,
            reason_code=SCHEDULE_SCRIPT_APPROVAL_AUTHORIZED_REASON,
            authorization_kind=SCHEDULE_SCRIPT_APPROVAL_AUTHORIZATION_KIND,
            authorization_ref=f"{occurrence.schedule_id}:{revision.revision}",
        )


class ScheduleScriptOffers:
    """Whether the app may offer a schedule the script approval."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, enabled: bool) -> None:
        """Keep the store that holds agent versions, and the operator's setting."""

        self._uow_factory = uow_factory
        self._enabled = enabled

    async def available(self, principal: Principal, revision: ScheduleRevision) -> bool:
        """Whether this owner could give this revision the approval.

        The setting is the operator's word that the schedule role holds the
        sandbox scope. A job runs no script, the owner cannot request a scope
        they do not hold, and the approval is for a tool the pinned agent has.
        """

        if (
            not self._enabled
            or isinstance(revision.task, JobTask)
            or SANDBOX_EXECUTE_SCOPE not in principal.scopes
        ):
            return False
        async with self._uow_factory() as uow:
            try:
                agent = await uow.agents.get_version(revision.agent_id, revision.agent_version)
            except NotFoundError:
                return False
        return SANDBOX_RUN_CODE_TOOL_NAME in agent.enabled_tools

    async def offered(self, principal: Principal, record: ScheduleRecord) -> ScheduleRecord:
        """The record as the app reads it, saying whether the approval can be given."""

        if not await self.available(principal, record.revision):
            return record
        return record.model_copy(update={"scripts_available": True})
