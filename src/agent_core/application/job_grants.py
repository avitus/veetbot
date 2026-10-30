"""Reuse private job access through the existing standing-authorization seam."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.jobs import (
    JobAccess,
    JobGrant,
    JobManifest,
    JobRecord,
    JobRevision,
    parse_job_access,
)
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind
from agent_core.ports.persistence import UnitOfWorkFactory


def access_needs_approval(access: JobAccess, saved: JobRecord) -> bool:
    """Require approval for remote access, new feed authority, or expanded existing limits."""
    if access.hosts or access.feeds or Decimal(access.model_cost) > 0:
        return True
    if not saved.grants:
        return bool(access.feeds)
    return not any(
        access.feeds <= grant.access.feeds
        and access.timeout_seconds <= grant.access.timeout_seconds
        and access.maximum_requests <= grant.access.maximum_requests
        and access.maximum_response_bytes <= grant.access.maximum_response_bytes
        for grant in saved.grants.values()
    )


def revision_grant_id(approval_id: UUID, revision_id: UUID) -> UUID:
    """Bind a new grant to approved code without changing stored record schemas."""
    return uuid5(approval_id, f"job-revision:{revision_id}")


def grant_covers_revision(grant: JobGrant, revision: JobRevision) -> bool:
    """Reuse external access only for its approved immutable code revision."""
    external = bool(
        grant.access.hosts or grant.access.feeds or Decimal(grant.access.model_cost) > 0
    )
    return not external or (
        grant.approval_id is not None
        and grant.id == revision_grant_id(grant.approval_id, revision.id)
    )


class JobGrantAuthorizer:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self.uow_factory = uow_factory

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
        """Reuse an owned grant or permit computation with no new external authority."""
        denied = StandingAuthorization(allowed=False, reason_code="job.grant.not_covered")
        if (
            action.name not in {"job.run", "job.manage"}
            or decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL
            or run.kind is not RunKind.INTERACTIVE
            or turn is None
            or turn.newest_user_trust is not TrustLevel.USER
        ):
            return denied
        try:
            if action.name == "job.manage":
                operation = action.arguments.get("operation")
                if operation == "read" or (
                    operation == "trial" and "grant_id" not in action.arguments
                ):
                    return StandingAuthorization(
                        allowed=True,
                        reason_code="job.preparation.authorized",
                        authorization_kind="job_preparation",
                        authorization_ref=str(run.id),
                    )
                if operation != "trial":
                    return denied
                async with self.uow_factory() as uow:
                    saved = await uow.jobs.get(principal, action.arguments["slug"])
                grant = saved.grants.get(str(action.arguments["grant_id"]))
                if (
                    saved.archived
                    or grant is None
                    or (
                        action.arguments.get("inputs")
                        and action.arguments["inputs"] != grant.access.inputs
                    )
                ):
                    return denied
                revision = next(
                    (
                        item
                        for item in saved.revisions.values()
                        if grant_covers_revision(grant, item)
                    ),
                    None,
                )
                external = bool(
                    grant.access.hosts or grant.access.feeds or Decimal(grant.access.model_cost) > 0
                )
                if external and (
                    revision is None
                    or revision.source != action.arguments.get("source")
                    or revision.manifest
                    != JobManifest.model_validate(action.arguments.get("manifest"))
                ):
                    return denied
                return StandingAuthorization(
                    allowed=True,
                    reason_code="job.grant.authorized",
                    authorization_kind="job_grant",
                    authorization_ref=str(grant.id),
                )
            async with self.uow_factory() as uow:
                saved = await uow.jobs.get(principal, action.arguments["slug"])
            revision = saved.revisions.get(str(action.arguments.get("revision_id")))
            if saved.archived or revision is None or revision.commit_sha is None:
                return denied
            if "grant_id" in action.arguments and "access" not in action.arguments:
                grant = saved.grants.get(str(action.arguments["grant_id"]))
            elif "access" in action.arguments and "grant_id" not in action.arguments:
                access = parse_job_access(action.arguments["access"])
                grant = next(
                    (
                        g
                        for g in saved.grants.values()
                        if g.access == access and grant_covers_revision(g, revision)
                    ),
                    None,
                )
                if (
                    grant is None
                    and not access_needs_approval(access, saved)
                    and action.origin_trust is TrustLevel.USER
                    and TrustLevel.EXTERNAL_UNTRUSTED not in action.argument_trust.values()
                ):
                    return StandingAuthorization(
                        allowed=True,
                        reason_code="job.local.authorized",
                        authorization_kind="job_local",
                        authorization_ref=str(revision.id),
                    )
            else:
                return denied
            if grant is None or not grant_covers_revision(grant, revision):
                return denied
            # Reading stored source marks the tool result untrusted. The exact
            # private grant remains the authority; it cannot expand with that data.
            return StandingAuthorization(
                allowed=True,
                reason_code="job.grant.authorized",
                authorization_kind="job_grant",
                authorization_ref=str(grant.id),
            )
        except (KeyError, ValueError, NotFoundError):
            return denied
