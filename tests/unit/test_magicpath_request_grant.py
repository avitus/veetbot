"""A MagicPath grant belongs to one owner request, catalog and sign-in."""

from datetime import timedelta

import pytest

from agent_core.application.magicpath_request_grants import MagicPathRequestGrants
from agent_core.domain.magicpath_request_grants import (
    MAGICPATH_GRANT_CREATED,
    MagicPathRequestGrantOffer,
)
from agent_core.domain.mcp import MCPServerAuthorization, MCPServerConfig
from agent_core.domain.policies import PolicyDecisionType
from tests.contract.support import NOW, principal
from tests.gates.test_owner_mcp_servers_adr0255 import METADATA
from tests.unit.test_job_request_grant import APPROVAL, OTHER_RUN, OWNER_TURN, a_run
from tests.unit.test_owner_mcp_servers import _owner
from tests.unit.test_script_request_grant import decision, proposed, world


@pytest.mark.parametrize(
    "case",
    [
        "same_request",
        "expired",
        "follow_up",
        "other_run",
        "new_login",
        "new_catalog",
        "new_policy",
        "denied",
        "other_endpoint",
        "no_magicpath_grant",
    ],
)
async def test_magicpath_request_boundaries(case: str) -> None:
    user = principal()
    catalog = "a" * 64
    offer = MagicPathRequestGrantOffer(
        tool_name="mcp.magicpath.create_canvas_shapes",
        signed_in_at=NOW,
        catalog_hash=catalog,
        project_id="123",
        summary="Create shapes in project 123.",
    )
    subject = await world(
        granted=True,
        approval_update={
            "tool_name": offer.tool_name,
            "script_request_grant_offer": offer,
        },
    )
    if case != "no_magicpath_grant":
        await subject.append(MAGICPATH_GRANT_CREATED, {"approval_id": str(APPROVAL)})
    if case == "follow_up":
        await subject.append("user.message.created", {"pending_input_id": "follow-up"})
    config = MCPServerConfig(**_owner(tenant_id=user.tenant_id))
    if case == "other_endpoint":
        config = config.model_copy(update={"endpoint": "https://example.test/mcp"})
    async with subject.uow_factory() as uow:
        await uow.mcp_servers.put(config)
        await uow.mcp_servers.put_authorization(
            MCPServerAuthorization(
                tenant_id=user.tenant_id,
                server_id="magicpath",
                metadata=METADATA,
                signed_in_at=NOW + timedelta(seconds=1) if case == "new_login" else NOW,
                signed_in_by=user.principal_id,
                access_token_sealed="fixture",
                updated_at=NOW,
            )
        )
    now = NOW + timedelta(hours=2) if case == "expired" else NOW
    grants = MagicPathRequestGrants(
        subject.uow_factory,
        lambda: now,
        lambda _session, _tool: "b" * 64 if case == "new_catalog" else catalog,
    )
    result = await grants.authorize(
        action=proposed(),
        decision=decision(
            "new-policy" if case == "new_policy" else "policy-v1",
            kind=PolicyDecisionType.DENY
            if case == "denied"
            else PolicyDecisionType.REQUIRE_APPROVAL,
        ),
        principal=user,
        run=a_run(id=OTHER_RUN) if case == "other_run" else a_run(),
        agent_version="agent-v1",
        action_deadline=now + timedelta(seconds=30),
        turn=OWNER_TURN,
    )
    assert result.allowed is (case == "same_request")
