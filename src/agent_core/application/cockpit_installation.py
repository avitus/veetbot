"""Bind a complete widget installation to one immutable preparation (ADR-0270)."""

from datetime import datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalRequest, ApprovalStatus
from agent_core.domain.canonical import canonical_json
from agent_core.domain.cockpit_installation import WidgetInstallation
from agent_core.domain.errors import NotFoundError
from agent_core.domain.policies import (
    AuthorizationTurn,
    PolicyDecision,
    PolicyDecisionType,
    ProposedAction,
    StandingAuthorization,
    TrustLevel,
)
from agent_core.domain.runs import Run, RunKind
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.ports.persistence import UnitOfWorkFactory

INSTALL_ARGUMENTS = {
    "type": "object",
    "properties": {
        "preparation_run_id": {"type": "string", "format": "uuid"},
        "preparation_id": {"type": "string", "format": "uuid"},
    },
    "required": ["preparation_run_id", "preparation_id"],
    "additionalProperties": False,
}


def installation_key(principal: Principal, preparation_id: UUID) -> str:
    return f"cockpit.install:{principal.tenant_id}:{principal.principal_id}:{preparation_id}"


async def prepared_installation(
    factory: UnitOfWorkFactory, principal: Principal, session_id: UUID, arguments: dict[str, Any]
) -> WidgetInstallation:
    """Resolve only a successful preparation from this owner's current conversation."""
    preparation_id = UUID(arguments["preparation_id"])
    async with factory() as uow:
        run = await uow.runs.get(UUID(arguments["preparation_run_id"]), principal)
        if run.session_id != session_id:
            raise ValueError("Prepare the installation in this conversation.")
        invocations = await uow.invocations.list_for_run(run.id, principal)
    for item in invocations:
        if (
            item.id == preparation_id
            and item.session_id == session_id
            and item.tool_name == "cockpit.prepare"
            and item.status is ToolInvocationStatus.SUCCEEDED
            and item.structured_result is not None
            and item.structured_result.get("ready") is True
            and item.normalized_arguments is not None
        ):
            return WidgetInstallation.model_validate(item.normalized_arguments)
    raise ValueError("Select a successful cockpit.prepare result before installing.")


async def installation_approval(
    factory: UnitOfWorkFactory,
    principal: Principal,
    session_id: UUID,
    arguments: dict[str, Any],
    invocation_id: UUID | None = None,
) -> ApprovalRequest | None:
    """Match either the current approval or the durable receipt for this exact proposal."""
    digest = sha256(canonical_json(arguments).encode("utf-8")).hexdigest()
    async with factory() as uow:
        approval = (
            None if invocation_id is None else await uow.approvals.get_by_action(invocation_id)
        )
        if approval is None:
            receipt = await uow.process_events.get_by_derivation(
                installation_key(principal, UUID(arguments["preparation_id"]))
            )
            if receipt is not None:
                approval = await uow.approvals.get(UUID(receipt.payload["approval_id"]), principal)
    if (
        approval is not None
        and approval.status is ApprovalStatus.APPROVED
        and approval.tenant_id == principal.tenant_id
        and approval.principal_id == principal.principal_id
        and approval.session_id == session_id
        and approval.tool_name == "cockpit.install"
        and approval.normalized_arguments_hash == digest
    ):
        return approval
    return None


class WidgetInstallationAuthorizer:
    def __init__(self, factory: UnitOfWorkFactory) -> None:
        self.factory = factory

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
        """Resume only the exact already-approved installation; never override a denial."""
        refused = StandingAuthorization(allowed=False, reason_code="cockpit.install.not_covered")
        if (
            action.name != "cockpit.install"
            or decision.decision is not PolicyDecisionType.REQUIRE_APPROVAL
            or run.kind is not RunKind.INTERACTIVE
            or turn is None
            or turn.newest_user_trust is not TrustLevel.USER
        ):
            return refused
        try:
            await prepared_installation(self.factory, principal, run.session_id, action.arguments)
            approval = await installation_approval(
                self.factory, principal, run.session_id, action.arguments
            )
        except (ValueError, KeyError, NotFoundError):
            return refused
        if approval is None:
            return refused
        return StandingAuthorization(
            allowed=True,
            reason_code="cockpit.install.authorized",
            authorization_kind="widget_installation",
            authorization_ref=str(approval.id),
        )
