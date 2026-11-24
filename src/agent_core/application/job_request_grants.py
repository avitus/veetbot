"""One approval for a request that changes a saved job (ADR-0344).

The approval the owner resolved with ``approve_for_request`` is the grant. The
authorizer finds it through the run's ``job.request_grant.created`` event,
checks the run, the window and the policy, and lets a covered job action
through without a card. Every refusal falls back to an ordinary approval.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from agent_core.application.browser_request_grants import approval_allowed_for_request
from agent_core.application.job_grants import revision_grant_id
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalRequest
from agent_core.domain.errors import NotFoundError
from agent_core.domain.job_request_grants import (
    JOB_REQUEST_GRANT_AUTHORIZATION_KIND,
    JOB_REQUEST_GRANT_AUTHORIZED_REASON,
    JOB_REQUEST_GRANT_CREATED_EVENT,
    JOB_REQUEST_GRANT_DURATION,
    JOB_REQUEST_GRANT_TOOLS,
    JOB_REQUEST_MANAGE_OPERATIONS,
    JOB_REQUEST_REPIN_FIELDS,
    MAXIMUM_JOB_REQUEST_CEILINGS,
    NO_JOB_REQUEST_GRANT,
    JobAccessCeiling,
    JobRequestGrantOffer,
    job_request_grant_summary,
    session_is_owner_chat,
)
from agent_core.domain.jobs import JobAccess, JobManifest, JobRecord, parse_job_access
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind
from agent_core.domain.schedules import JobTask
from agent_core.domain.sessions import Session
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JobRequestGrantResolution:
    """What the public approval service needs to allow a job request: the
    clock that stamps the grant's window."""

    clock: Clock


def run_allows_job_request_grant(run: Run, session: Session, principal: Principal) -> bool:
    """A top-level interactive run of the principal's own chat."""

    return (
        run.kind is RunKind.INTERACTIVE
        and run.parent_run_id is None
        and run.tenant_id == principal.tenant_id
        and (session.tenant_id, session.principal_id)
        == (principal.tenant_id, principal.principal_id)
        and session_is_owner_chat(session.metadata)
    )


def target_slug(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """The job a covered tool acts on, read from its arguments."""

    if tool_name == "schedule.update":
        task = arguments.get("task")
        slug = task.get("slug") if isinstance(task, dict) else None
    else:
        slug = arguments.get("slug")
    return slug if isinstance(slug, str) else None


async def _saved(uow: RepositoryUnitOfWork, principal: Principal, slug: str) -> JobRecord | None:
    try:
        saved = await uow.jobs.get(principal, slug)
    except NotFoundError:
        return None
    return None if saved.archived else saved


def _card_access(saved: JobRecord, tool_name: str, arguments: dict[str, Any]) -> JobAccess | None:
    """The access the pending action itself would use, if it names one."""

    try:
        if tool_name == "job.run" and "access" in arguments:
            return parse_job_access(arguments["access"])
        grant_id: object = (
            JobTask.model_validate(arguments["task"]).grant_id
            if tool_name == "schedule.update"
            else arguments.get("grant_id")
        )
        grant = saved.grants.get(str(grant_id)) if grant_id is not None else None
    except (KeyError, ValueError):
        return None
    return None if grant is None else grant.access


async def job_request_not_covered(
    uow: RepositoryUnitOfWork,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    principal: Principal,
    offer: JobRequestGrantOffer,
    approval_id: UUID | None,
) -> str | None:
    """Why the offer does not cover the action, as a closed reason, or none."""

    if tool_name not in JOB_REQUEST_GRANT_TOOLS:
        return "operation"
    if tool_name == "schedule.update" and not set(arguments) <= JOB_REQUEST_REPIN_FIELDS:
        # Timing changes and server replacement still ask (ADR-0344 decision 2).
        return "schedule"
    if (
        tool_name == "job.manage"
        and arguments.get("operation") not in JOB_REQUEST_MANAGE_OPERATIONS
    ):
        return "operation"
    if target_slug(tool_name, arguments) != offer.slug:
        return "other_job"
    saved = await _saved(uow, principal, offer.slug)
    if saved is None:
        return "unavailable"
    if tool_name == "job.manage":
        if arguments.get("operation") == "update":
            try:
                manifest = JobManifest.model_validate(arguments.get("manifest"))
            except ValueError:
                return "unavailable"
            if not any(manifest.feeds <= set(ceiling.feeds) for ceiling in offer.ceilings):
                return "access"
            return None
        grant = saved.grants.get(str(arguments.get("grant_id")))
        if grant is None:
            return "operation" if "grant_id" not in arguments else "unavailable"
        if "inputs" in arguments and arguments["inputs"] != grant.access.inputs:
            return "access"
        return None if offer.covers(grant.access) else "access"
    if tool_name == "schedule.update":
        try:
            task = JobTask.model_validate(arguments["task"])
            schedule_id = UUID(str(arguments["schedule_id"]))
            schedule = await uow.schedules.get(schedule_id, principal)
            current = await uow.schedules.get_revision(
                schedule_id, schedule.current_revision, principal
            )
        except (KeyError, ValueError, NotFoundError):
            return "unavailable"
        if not isinstance(current.task, JobTask) or current.task.slug != offer.slug:
            return "schedule"
        revision = saved.revisions.get(str(task.revision_id))
        grant = saved.grants.get(str(task.grant_id))
        if revision is None or revision.commit_sha is None or grant is None:
            return "unavailable"
        return None if offer.covers(grant.access) else "access"
    revision = saved.revisions.get(str(arguments.get("revision_id")))
    access = _card_access(saved, tool_name, arguments)
    if revision is None or revision.commit_sha is None or access is None:
        return "unavailable"
    if not offer.covers(access):
        return "access"
    if approval_id is not None:
        # ADR-0344 decision 4: a run binds its new grant to the request
        # approval; that id must not already hold other access.
        bound = saved.grants.get(str(revision_grant_id(approval_id, revision.id)))
        if bound is not None and bound.access != access:
            return "grant_collision"
    return None


async def job_request_grant_offer(
    uow: RepositoryUnitOfWork,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    run: Run,
    principal: Principal,
    turn: AuthorizationTurn | None,
) -> JobRequestGrantOffer | None:
    """The card's offer, or none: an owner-driven run of the owner's chat, and
    an action the grant would itself cover (ADR-0344 decision 1)."""

    slug = target_slug(tool_name, arguments)
    if (
        tool_name not in JOB_REQUEST_GRANT_TOOLS
        or slug is None
        or turn is None
        or turn.newest_user_trust is not TrustLevel.USER
    ):
        return None
    try:
        session = await uow.sessions.get(run.session_id, principal)
    except NotFoundError:
        return None
    saved = await _saved(uow, principal, slug)
    if saved is None or not run_allows_job_request_grant(run, session, principal):
        return None
    card = _card_access(saved, tool_name, arguments)
    accesses = ([card] if card is not None else []) + [
        grant.access for grant in saved.grants.values()
    ]
    # Card first, then the job's grants, without repeats.
    ceilings: tuple[JobAccessCeiling, ...] = ()
    for item in accesses:
        ceiling = JobAccessCeiling.from_access(item)
        if ceiling not in ceilings:
            ceilings += (ceiling,)
    if not ceilings:
        return None
    offer = JobRequestGrantOffer(
        slug=slug,
        ceilings=ceilings[:MAXIMUM_JOB_REQUEST_CEILINGS],
        summary=job_request_grant_summary(slug),
    )
    reason = await job_request_not_covered(
        uow,
        tool_name=tool_name,
        arguments=arguments,
        principal=principal,
        offer=offer,
        approval_id=None,
    )
    return offer if reason is None else None


async def job_request_approval_for_run(
    uow: RepositoryUnitOfWork, run: Run, principal: Principal
) -> ApprovalRequest | None:
    """The run's job request approval, found through its creation event."""

    approval = await approval_allowed_for_request(
        uow, run, principal, JOB_REQUEST_GRANT_CREATED_EVENT
    )
    if approval is None or approval.job_request_grant_offer is None:
        return None
    return approval


class JobRequestGrantAuthorizer:
    """Authorize a job action the owner allowed for the whole request."""

    def __init__(self, *, uow_factory: UnitOfWorkFactory, now: Callable[[], datetime]) -> None:
        """Keep the store and the clock the checks read."""

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
        """Allow the action only when this run's job request grant covers it."""

        del action_deadline
        if (
            action.name not in JOB_REQUEST_GRANT_TOOLS
            or decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL
        ):
            return StandingAuthorization(allowed=False, reason_code=NO_JOB_REQUEST_GRANT)
        now = self._now()
        async with self._uow_factory() as uow:
            approval = await job_request_approval_for_run(uow, run, principal)
            if approval is None:
                return StandingAuthorization(allowed=False, reason_code=NO_JOB_REQUEST_GRANT)
            offer = approval.job_request_grant_offer
            assert offer is not None and approval.resolved_at is not None
            try:
                session: Session | None = await uow.sessions.get(run.session_id, principal)
            except NotFoundError:
                session = None
            if (
                session is None
                or not run_allows_job_request_grant(run, session, principal)
                or turn is None
                or turn.newest_user_trust is not TrustLevel.USER
            ):
                return _not_covered(approval, "run_not_eligible")
            if now >= approval.resolved_at + JOB_REQUEST_GRANT_DURATION:
                return _not_covered(approval, "expired")
            if agent_version != approval.agent_version:
                return _not_covered(approval, "agent_changed")
            if decision.policy_version != approval.policy_version:
                return _not_covered(approval, "policy_changed")
            reason = await job_request_not_covered(
                uow,
                tool_name=action.name,
                arguments=action.arguments,
                principal=principal,
                offer=offer,
                approval_id=approval.id,
            )
        if reason is not None:
            return _not_covered(approval, reason)
        return StandingAuthorization(
            allowed=True,
            reason_code=JOB_REQUEST_GRANT_AUTHORIZED_REASON,
            authorization_kind=JOB_REQUEST_GRANT_AUTHORIZATION_KIND,
            authorization_ref=str(approval.id),
        )


def _not_covered(approval: ApprovalRequest, reason: str) -> StandingAuthorization:
    """A refusal naming the closed reason and the approval that holds the grant."""

    code = f"job.request_grant.{reason}"
    # Content-free: a closed reason and the approval that holds the grant.
    logger.info(
        "job_request_grant_not_covered",
        extra={"reason_code": code, "approval_id": str(approval.id)},
    )
    return StandingAuthorization(
        allowed=False,
        reason_code=code,
        authorization_kind=JOB_REQUEST_GRANT_AUTHORIZATION_KIND,
        authorization_ref=str(approval.id),
    )
