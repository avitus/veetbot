"""One owner approval for project discovery, a script, and repeated canvas edits."""

from pathlib import Path

import pytest

from agent_core.domain.mcp import (
    MCPDiscovery,
    MCPRemoteTool,
    MCPServerAccess,
    ScriptedMCPResponse,
    ScriptedMCPServer,
)
from agent_core.domain.messages import ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.evals.code_mode import ScriptExecutionEnvironment
from tests.gates.test_deferred_tools_adr0123 import _production
from tests.gates.test_owner_mcp_servers_adr0255 import (
    ENABLED,
    _add_and_sign_in,
    _magicpath_gateway,
)
from tests.integration.journey_support import api_client


@pytest.mark.parametrize(
    "last_name, last_project",
    [
        ("create_canvas_shapes", "456"),
        ("delete_canvas_shapes", "123"),
    ],
)
async def test_one_card_covers_the_magicpath_design_request(
    tmp_path: Path,
    last_name: str,
    last_project: str,
) -> None:
    operations: list[tuple[str, dict[str, str]]] = [
        ("get_active_projects", {}),
        ("list_projects", {}),
        ("sandbox.run_code", {"code": "print(tools.call('mcp.magicpath.list_projects', {}))"}),
        ("get_canvas_context", {"projectId": "123"}),
        ("create_canvas_shapes", {"projectId": "123"}),
        ("create_canvas_shapes", {"projectId": "123"}),
        (last_name, {"projectId": last_project}),
    ]
    turns = []
    responses = []
    for name, arguments in operations:
        if name == "sandbox.run_code":
            responses.append(ScriptedMCPResponse(name="list_projects"))
            call = ScriptedToolCall(name=name, arguments=arguments)
        else:
            call = ScriptedToolCall(
                name="tool.call",
                arguments={"name": f"mcp.magicpath.{name}", "arguments": arguments},
            )
            responses.append(ScriptedMCPResponse(name=name))
        turns.append(ScriptedTurn(tool_calls=[call], stop_reason=StopReason.TOOL_USE))
    schema = {"type": "object", "properties": {"projectId": {"type": "string"}}}
    methods = dict.fromkeys(response.name for response in responses if response.name)
    server = ScriptedMCPServer(
        name="magicpath",
        discovery=MCPDiscovery(
            tools=tuple(MCPRemoteTool(name=name, input_schema=schema) for name in methods)
        ),
        responses=tuple(responses),
    )
    factory, context = await _production(
        tmp_path,
        turns,
        extra_environment={**ENABLED, "AGENT_CODE_MODE_ENABLED": "1"},
        extra_scripts={"magicpath": server},
        mcp_oauth_gateway=_magicpath_gateway(),
        fake_environment_factory=ScriptExecutionEnvironment,
    )
    async with context as app, api_client(app) as api:
        await _add_and_sign_in(app, MCPServerAccess.CHANGES)
        session = await app.sessions.create()
        run = await app.runs.submit("Draw the design in MagicPath.", session)
        [card] = await app.approvals.list_pending(run_id=run)
        assert card.tool_name == "mcp.magicpath.get_active_projects"
        path = f"/v1/approvals/{card.id}/resolve"
        visible = (await api.get(f"/v1/approvals/{card.id}")).json()
        offer = visible["script_request_grant_offer"]
        assert offer is not None and "MagicPath" in offer["summary"]
        wrong = await api.post(
            path,
            json={
                "decision": "approve_for_request",
                "request_grant": {"tool_name": "sandbox.run_code"},
            },
        )
        assert wrong.status_code == 409
        body = {
            "decision": "approve_for_request",
            "request_grant": {"tool_name": offer["tool_name"]},
        }
        allowed = await api.post(path, json=body)
        assert allowed.status_code == 200, allowed.text
        assert (await api.post(path, json=body)).status_code == 200
        [outside_grant] = await app.approvals.list_pending(run_id=run)
        assert outside_grant.tool_name == f"mcp.magicpath.{last_name}"
        assert outside_grant.arguments.get("projectId") == last_project
        if last_name == "delete_canvas_shapes":
            assert outside_grant.script_request_grant_offer is None
        else:
            assert outside_grant.script_request_grant_offer is not None
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(session, 0, app.principal, run_id=run)
        assert sum(client.call_count for client in factory.created) == 6
        assert not [e for e in events if e.event_type in {"tool.call.failed", "tool.call.denied"}]
        uses = [
            e.payload
            for e in events
            if e.event_type == "tool.call.authorized"
            and e.payload.get("authorization_kind") == "magicpath_request_grant"
        ]
        assert len(uses) == 6
        assert all(use["authorization_ref"] == str(card.id) for use in uses)
